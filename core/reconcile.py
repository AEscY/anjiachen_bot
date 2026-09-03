"""
启动对账 —— 防止"账本丢失 → 重复买入 / 止损失效"

背景
────
持仓量取自本地 SQLite 账本（position_lots / grid.lots），而非交易所余额。
一旦数据库丢失或回档（Render 免费层临时盘、Serv00 停机、容器重建都可能触发），
机器人会认为"我没有持仓"，而交易所里的币其实还在：

    → 它会再买一遍，仓位翻倍
    → 更糟的是止损也读本地账本，看不到真实持仓，止损永不触发

本模块在启动时拿交易所真实余额与本地账本比对，对不上就
**暂停该币种交易并告警**，而不是默默当成空仓继续跑。

设计原则
────────
1. 只告警、只暂停，绝不自动改账本 —— 差异原因可能很复杂，
   自动"以交易所为准"会掩盖真问题（比如多账户共用 API key）。
2. 容差要宽松：交易所精度、手续费扣币、在途挂单都会造成合理差异。
3. 对账失败（网络异常）不阻塞启动 —— 降级为"未知"，不暂停。
"""

from __future__ import annotations

import time
from typing import Optional

from config import logger

# 单个币种的对账结果
LEVEL_OK = "ok"          # 一致
LEVEL_DRIFT = "drift"    # 有差异但在容差内（可能是手续费/精度，不阻塞）
LEVEL_MISMATCH = "mismatch"  # 显著差异，暂停交易
LEVEL_UNKNOWN = "unknown"    # 无法判断（网络/接口异常），不阻塞


class ReconcileResult:
    __slots__ = ("symbol", "level", "local_qty", "exchange_qty",
                 "diff", "tolerance", "message", "ts")

    def __init__(self, symbol, level, local_qty, exchange_qty,
                 diff, tolerance, message):
        self.symbol = symbol
        self.level = level
        self.local_qty = local_qty
        self.exchange_qty = exchange_qty
        self.diff = diff
        self.tolerance = tolerance
        self.message = message
        self.ts = time.time()

    @property
    def blocking(self) -> bool:
        return self.level == LEVEL_MISMATCH

    def __repr__(self):
        return (f"<Reconcile {self.symbol} {self.level} "
                f"local={self.local_qty:.6f} exch={self.exchange_qty:.6f} "
                f"diff={self.diff:.6f} tol={self.tolerance:.6f}>")


class Reconciler:
    """
    对账器。

    容差取三者最大：
      - 交易所最小下单精度（round_amount 反推）
      - 本地持仓的 rel_tol 比例（默认 2%）
      - 一个绝对下限（默认 0.5% 的持仓量，防止小额持仓被比例容差放过）

    为什么是 2%：
      买入时以币扣手续费会减少净额，网格每档独立，
      多档累计后差异通常在 1% 内。2% 留足余量，同时
      仍能抓住"账本为空但交易所有一整仓"这种致命情况。
    """

    def __init__(self, bot):
        self.bot = bot
        self.results: dict = {}
        self.blocked: set = set()
        # 容差参数（可被 params 注册表覆盖）
        self.rel_tol = 0.02
        self.abs_floor = 0.005
        self.enabled = True

    # ─────────── 本地持仓汇总 ───────────

    def _local_qty(self, sym: str) -> float:
        """合并统计两种模式的本地持仓"""
        total = 0.0
        # 单次模式：FIFO 账本
        for lot in self.bot.position_lots.get(sym, []):
            # ⚠️ 跨文件遗漏：v17 只统一了 bot.py 内部的读取，
            # 忘了 reconcile.py —— 这里是【对账】读本地持仓量的地方。
            # 若读成 0 而交易所有持仓，会误判 mismatch 并阻塞交易。
            # 必须走 bot 的统一辅助函数，与下单/平仓口径保持一致。
            get_amt = getattr(self.bot, "_lot_amount", None)
            try:
                if get_amt is not None:
                    total += float(get_amt(lot) or 0)
                else:
                    # KEYCONTRACT-OK: 有意保留的降级分支。
                    # 正常路径走 bot._lot_amount；此处仅用于
                    # Reconciler 被单独实例化（无 bot 辅助函数）时，
                    # 与统一函数保持同样的兼容语义。
                    v = lot.get("amount")
                    if v is None:
                        v = lot.get("qty")
                    total += float(v or 0)
            except (TypeError, ValueError):
                continue
        # 网格模式：每档持仓
        try:
            st = self.bot.grid.get_state(sym)
            if st and getattr(st, "lots", None):
                for lot in st.lots.values():
                    try:
                        total += float(lot.get("qty", 0) or 0)
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass
        return total

    # ─────────── 容差计算 ───────────

    def _probe_step(self, sym: str) -> Optional[float]:
        """
        取该币种的最小下单精度（amount precision step）。

        不能用 round_amount(sym, 1.0) 反推 —— 那个返回值是
        "1.0 按精度取整后的结果"，当精度很细时会得到极小值，
        再取倒数会把容差放大成天文数字，导致对账完全失效。
        必须直接读 markets 里的 precision。
        """
        try:
            markets = getattr(self.bot.exchange.exchange, "markets", None)
            if markets:
                m = markets.get(sym)
                if m:
                    prec = (m.get("precision") or {}).get("amount")
                    if prec:
                        return float(prec)
        except Exception:
            pass
        return None

    def _probe_min_amount(self, sym: str) -> Optional[float]:
        """取该币种的最小下单量"""
        try:
            markets = getattr(self.bot.exchange.exchange, "markets", None)
            if markets:
                m = markets.get(sym)
                if m:
                    lim = ((m.get("limits") or {}).get("amount") or {}).get("min")
                    if lim:
                        return float(lim)
        except Exception:
            pass
        return None

    async def _tolerance(self, sym: str, local_qty: float) -> float:
        """
        容差 = max(持仓×相对容差, 最小精度×10, 最小下单量×3)

        三个来源都必须**随精度变小而变小**，绝不能取倒数放大。
        """
        tol = abs(local_qty) * self.rel_tol

        step = self._probe_step(sym)
        if step and step > 0:
            tol = max(tol, step * 10.0)

        if local_qty > 0:
            tol = max(tol, local_qty * self.abs_floor)
        else:
            # 本地为空：允许"灰尘级"余额，
            # 用最小下单量的 3 倍兜底（拿不到就用最小精度的 10 倍）
            min_amt = self._probe_min_amount(sym)
            if min_amt and min_amt > 0:
                tol = max(tol, min_amt * 3.0)
            elif step and step > 0:
                tol = max(tol, step * 10.0)
            else:
                tol = max(tol, 1e-8)

        # 硬上限：容差绝不超过本地持仓的 20%，
        # 防止任何异常精度值把容差放大到让对账失效
        if local_qty > 0:
            tol = min(tol, local_qty * 0.2)
        return max(tol, 1e-8)

    # ─────────── 单币种对账 ───────────

    async def check_symbol(self, sym: str) -> ReconcileResult:
        if not self.enabled:
            return ReconcileResult(sym, LEVEL_UNKNOWN, 0, 0, 0, 0, "对账已关闭")

        local_qty = self._local_qty(sym)

        # 取交易所该币余额
        try:
            base = sym.split("/")[0]
            bal = await self.bot.exchange.fetch_balance()
            entry = bal.get(base)
            exch_qty = float(entry.get("total", 0) or 0) if entry else 0.0
        except Exception as e:
            return ReconcileResult(
                sym, LEVEL_UNKNOWN, local_qty, 0, 0, 0,
                f"无法读取交易所余额({type(e).__name__})，跳过对账")

        tol = await self._tolerance(sym, local_qty)
        diff = abs(exch_qty - local_qty)

        if diff <= tol:
            lvl, msg = LEVEL_OK, ""
        elif local_qty <= tol and exch_qty > tol:
            # 最危险的情况：本地认为空仓，交易所却有币
            lvl = LEVEL_MISMATCH
            msg = (f"本地账本为空，但交易所有 {exch_qty:.6f} {base}。"
                   f"可能数据库丢失/回档，已暂停该币交易以防重复买入")
        elif exch_qty <= tol and local_qty > tol:
            lvl = LEVEL_MISMATCH
            msg = (f"本地账本有 {local_qty:.6f} {base}，但交易所为 0。"
                   f"持仓可能已被外部平掉，已暂停该币交易以防止损失效")
        elif diff <= tol * 3:
            lvl = LEVEL_DRIFT
            msg = (f"轻微偏差 {diff:.6f}（本地 {local_qty:.6f} / "
                   f"交易所 {exch_qty:.6f}），可能是手续费或精度，继续运行")
        else:
            lvl = LEVEL_MISMATCH
            msg = (f"持仓对不上：本地 {local_qty:.6f} vs 交易所 {exch_qty:.6f}"
                   f"（差 {diff:.6f} > 容差 {tol:.6f}），已暂停该币交易")

        r = ReconcileResult(sym, lvl, local_qty, exch_qty, diff, tol, msg)
        self.results[sym] = r
        if r.blocking:
            self.blocked.add(sym)
        else:
            self.blocked.discard(sym)
        return r

    # ─────────── 全量对账 ───────────

    async def check_all(self) -> list:
        results = []
        for sym in getattr(self.bot, "symbols", []) or []:
            try:
                results.append(await self.check_symbol(sym))
            except Exception as e:
                logger.warning(f"对账 {sym} 异常: {e}")
                results.append(ReconcileResult(
                    sym, LEVEL_UNKNOWN, 0, 0, 0, 0, f"对账异常: {type(e).__name__}"))
        return results

    def is_blocked(self, sym: str) -> bool:
        return sym in self.blocked

    def summary(self) -> str:
        if not self.results:
            return "尚未对账"
        lines = []
        icon = {LEVEL_OK: "✅", LEVEL_DRIFT: "⚠️",
                LEVEL_MISMATCH: "🚨", LEVEL_UNKNOWN: "❓"}
        for sym, r in self.results.items():
            base = sym.split("/")[0]
            lines.append(
                f"{icon.get(r.level,'❓')} {base:<6} "
                f"本地 {r.local_qty:<12.6f} 交易所 {r.exchange_qty:<12.6f} {r.level}")
            if r.message:
                lines.append(f"      {r.message}")
        return "\n".join(lines)

    def clear(self, sym: Optional[str] = None):
        """解除暂停（人工确认后）"""
        if sym:
            self.blocked.discard(sym)
            self.results.pop(sym, None)
        else:
            self.blocked.clear()
            self.results.clear()
