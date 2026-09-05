"""
主循环 + Telegram 控制。

设计原则（每一条都对应旧版踩过的坑）：

1. 只在【完全成交】时记账
   旧版在部分成交时按整单成本平摊，导致买入10卖3时
   赚 6U 被记成亏 694U。这里只在 status == 'closed' 时处理，
   未成交部分继续挂着等成交。

2. 订单状态以交易所为准
   每轮对比本地记录与交易所未成交列表。本地以为挂着、
   实际已成交的单子会被正确识别 —— 不依赖回调。

3. 权益口径唯一
   不设 cap，不做缩放。旧版因 cap 与真实权益混用
   导致回撤算成 99.99%，永久熔断且无日志。

4. 每格金额单一实现
   挂单与 /status 显示调用同一个函数。旧版两处各算一遍，
   报告说 3.60U「会挂单」，实际挂 12070U（超额 14.5 倍）。

5. 不用 getattr 兜底
   参数名写错就该立刻崩溃。旧版 84 处 getattr 兜底让
   equity_cap 拼写错误静默溜到线上，三个币种全挂。
"""
import asyncio
import logging
import os
import time

from telegram import Bot

import config as C
import notify
import store
from exchange import Exchange
from grid import below_stop, buy_prices, effective_sell, sell_price
from params import Params
from risk import Risk

logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self):
        self.state = store.load(C.STATE_FILE, store.empty_state())
        self.p = Params(self.state.get("params"))
        self.risk = Risk(self.state["risk"], self.p)
        # 认证失败等致命错误必须立刻送达 —— 用同步推送，
        # 因为此处还没有事件循环，_tell() 用不了
        self.ex = Exchange(on_fatal=notify.push)
        self.tg = Bot(token=C.TG_TOKEN)
        self.running = bool(self.state.get("running", True))
        self._last_save = 0.0
        self._syms: list[str] = []

        syms = self.state.get("coins")
        if syms:
            self._syms = list(syms.keys())
        else:
            self._syms = [s.strip().upper() for s in
                          os.getenv("SYMBOLS", "SOL/USDT").split(",")
                          if s.strip()]
            for s in self._syms:
                self.state["coins"][s] = self._new_coin(0.0)

    # ─────────── 币种状态 ───────────

    @staticmethod
    def _new_coin(center: float) -> dict:
        return {"center": center, "lots": {}, "orders": {}}

    def _coin(self, sym: str, price: float = 0.0) -> dict:
        """取币种状态，不存在则新建。center=0 表示尚未初始化。"""
        st = self.state["coins"].get(sym)
        if st is None:
            st = self._new_coin(0.0)
            self.state["coins"][sym] = st
        if st["center"] <= 0 and price > 0:
            st["center"] = price
            logger.info(f"{sym} 网格中枢初始化为 {price}")
        return st

    # ─────────── 资金 ───────────

    def per_grid(self, free_usdt: float) -> float:
        """
        每档金额。挂单与状态显示【共用此函数】。

        = (可用现金 − 保留底线) × 使用率 ÷ (币种数 × 层数)

        旧版用"总权益"算，而买单只能花现金 ——
        账户 88731U 权益但只有 4998U 现金时，算出每格 12070U，
        超额 14.5 倍。
        """
        n = max(1, len(self.state["coins"]))
        levels = max(1, int(self.p.get("levels")))
        budget = max(0.0, (free_usdt - self.p.get("reserve"))
                     * self.p.get("capital_pct"))
        return budget / (n * levels)

    # ─────────── 成交处理 ───────────

    def _on_fill(self, sym: str, st: dict, key: str, order: dict) -> None:
        side, lvl = key.split(":", 1)
        price = float(order.get("average") or 0.0) or float(order.get("price") or 0.0)
        qty = float(order.get("filled") or 0.0)
        if qty <= 0 or price <= 0:
            logger.error(f"{sym} 成交数据异常，跳过: {order}")
            return

        spacing = self.p.get("spacing")

        if side == "buy":
            st["lots"][lvl] = {
                "qty": qty,
                "cost_usdt": qty * price,
                "buy_price": price,
                "sell_price": sell_price(price, spacing),
                "buy_time": time.time(),
            }
            self._tell(f"🟢 买入 {sym}\n"
                       f"   {qty} @ {price}\n"
                       f"   卖单挂 {sell_price(price, spacing):.4f}")
        else:
            lot = st["lots"].pop(lvl, None)
            if lot is None:
                logger.warning(f"{sym} 卖出成交但无对应持仓: {key}")
                return
            pnl = qty * price - float(lot["cost_usdt"])
            self.risk.add_realized(pnl)
            self._tell(f"🔴 卖出 {sym}\n"
                       f"   {qty} @ {price}\n"
                       f"   净利 {pnl:+.4f}U")

    def _sync(self, sym: str, st: dict) -> None:
        """对比交易所未成交列表，处理已成交/已撤销的订单。"""
        live = {str(o["id"]) for o in self.ex.open_orders(sym)}
        for key in list(st["orders"]):
            rec = st["orders"][key]
            oid = str(rec["id"])
            if oid in live:
                continue
            o = self.ex.fetch_order(oid, sym)
            if o is None:
                del st["orders"][key]
                continue
            if o.get("status") == "closed" or float(o.get("filled") or 0) > 0:
                self._on_fill(sym, st, key, o)
            del st["orders"][key]

    # ─────────── 挂单 ───────────

    def _place(self, sym: str, st: dict, price: float, per_grid: float) -> None:
        levels = int(self.p.get("levels"))
        spacing = self.p.get("spacing")
        min_order = self.p.get("min_order")
        min_amt = self.ex.min_amount(sym)

        for i in range(levels):
            lvl = str(i)
            bp = buy_prices(st["center"], spacing, levels)[i]
            bp = self.ex.round_price(sym, bp)
            lot = st["lots"].get(lvl)

            if lot is None:
                key = f"buy:{lvl}"
                if price <= bp:
                    continue                       # 已跌破买档，等回调
                if key in st["orders"]:
                    continue                       # 已挂
                if per_grid < min_order:
                    continue
                qty = self.ex.round_qty(sym, per_grid / bp)
                if qty <= 0 or (min_amt and qty < min_amt):
                    continue
                oid = self.ex.limit_buy(sym, qty, bp)
                if oid:
                    st["orders"][key] = {"id": oid, "price": bp, "qty": qty}
                    logger.info(f"{sym} 挂买单 档{i} {qty}@{bp}")
            else:
                key = f"sell:{lvl}"
                sp = effective_sell(
                    lot, price, spacing,
                    follow=self.p.get("follow_hours") > 0,
                    follow_hours=self.p.get("follow_hours"),
                    follow_max_loss=self.p.get("follow_max_loss"),
                )
                sp = self.ex.round_price(sym, sp)

                rec = st["orders"].get(key)
                if rec and abs(rec["price"] - sp) <= sp * 1e-6:
                    continue                       # 价格没变，不用动
                if rec:
                    self.ex.cancel(rec["id"], sym)
                    del st["orders"][key]

                qty = self.ex.round_qty(sym, lot["qty"])
                if qty <= 0 or (min_amt and qty < min_amt):
                    continue
                oid = self.ex.limit_sell(sym, qty, sp)
                if oid:
                    st["orders"][key] = {"id": oid, "price": sp, "qty": qty}
                    logger.info(f"{sym} 挂卖单 档{i} {qty}@{sp}")

    def _liquidate(self, sym: str, st: dict, price: float) -> None:
        """区间止损：市价清仓并重置网格。"""
        sold = 0.0
        pnl = 0.0
        for lvl in list(st["lots"]):
            lot = st["lots"].pop(lvl)
            qty = self.ex.round_qty(sym, lot["qty"])
            if qty <= 0:
                continue
            self.ex.market_sell(sym, qty)
            pnl += qty * price - float(lot["cost_usdt"])
            sold += qty
        for key, rec in st["orders"].items():
            self.ex.cancel(rec["id"], sym)
        st["orders"] = {}

        if sold > 0:
            self.risk.add_realized(pnl)
            self._tell(f"🚨 {sym} 跌破区间止损，已清仓\n"
                       f"   卖出 {sold} @ 约 {price}\n"
                       f"   净利 {pnl:+.4f}U")

        st["center"] = price
        logger.info(f"{sym} 网格中枢重置为 {price}")

    # ─────────── 主循环 ───────────

    async def step(self) -> None:
        free_usdt, coins = self.ex.balances()
        if free_usdt <= 0 and not coins:
            logger.warning("取不到余额，跳过本轮")
            return

        prices = {s: self.ex.price(s) for s in self.state["coins"]}
        equity = free_usdt + sum(
            q * prices.get(f"{c}/USDT", 0.0) for c, q in coins.items())
        self.risk.update(equity)

        # 风控只管【开新仓】，不管【记账】和【止损】。
        #
        # 曾经这里写成 `if not can_open: return`，把整个循环跳过了 ——
        # 结果暂停期间成交的买单不进账本：
        #
        #     交易所持仓 8.24，本地账本 0.00
        #     → 账本与交易所不一致，重启即触发对账阻塞
        #
        # 端到端测试抓到的（test_e2e 下跌场景，差 8.24 个币）。
        # 这跟旧版"熔断把止损也停了"是同一类错误：
        # 保护措施反而制造了更危险的状态。
        blocked = not self.running or not self.risk.can_open(equity)
        if blocked and self.risk.reason:
            logger.info(f"风控拦截（仅停止开新仓）: {self.risk.reason}")

        per_grid = self.per_grid(free_usdt)

        for sym in list(self.state["coins"]):
            price = prices.get(sym, 0.0)
            if price <= 0:
                logger.warning(f"{sym} 取不到价格，跳过")
                continue
            st = self._coin(sym, price)

            # ① 成交记账 —— 永远执行
            self._sync(sym, st)

            # ② 区间止损 —— 永远执行（保护优先于暂停）
            if below_stop(price, st["center"], self.p.get("stop_loss")):
                self._liquidate(sym, st, price)
                continue

            # ③ 开新仓 —— 受风控控制
            if blocked:
                continue
            self._place(sym, st, price, per_grid)

        if time.time() - self._last_save > 30:
            self.save()
            self._last_save = time.time()

    async def check_consistency(self) -> None:
        """
        启动时对账：交易所持仓 vs 本地账本。

        只告警，【不阻塞】。

        旧版 v14 的做法是"发现不一致就暂停交易等人工处理"，
        但没给解锁工具 —— 用户每次重启都要手工介入，形成死锁。
        这里改为：告警 + 告知交易所是权威数据源。

        正常运行下本函数不会报差异（_sync 以交易所订单状态为准），
        差异只来自：手动在交易所买卖、旧版遗留持仓、数据库丢失。
        """
        _, coins = self.ex.balances()
        bad = []
        for sym, st in self.state["coins"].items():
            base = sym.split("/")[0]
            exch = float(coins.get(base, 0.0))
            local = sum(float(l.get("qty", 0)) for l in st["lots"].values())
            tol = max(1e-8, local * 0.01)
            if abs(exch - local) > tol:
                bad.append(f"  {sym}: 本地 {local:.6f} / 交易所 {exch:.6f}")
        if bad:
            await self._tell(
                "🚨 启动对账发现差异\n" + "\n".join(bad) +
                "\n\n交易所是权威数据源。差异通常来自手动交易或旧版遗留。"
                "\n如需让机器人接管，请先在交易所处理，再 /recenter 重设中枢。")
            logger.warning("启动对账差异:\n" + "\n".join(bad))

    async def loop(self) -> None:
        await self.check_consistency()
        while True:
            try:
                await self.step()
            except Exception as e:
                logger.exception(f"主循环异常: {e}")
                await self._tell(f"⚠️ 主循环异常\n{e}")
            await asyncio.sleep(max(3, int(self.p.get("poll"))))

    # ─────────── 持久化 ───────────

    def save(self) -> None:
        self.state["params"] = self.p.dump()
        self.state["running"] = self.running
        if not store.save(C.STATE_FILE, self.state):
            self._tell_nowait("🚨 状态保存失败，重启将丢失持仓记录")

    async def backup(self) -> None:
        data = store.to_backup_bytes(self.state)
        await self.tg.send_document(
            chat_id=C.TG_CHAT_ID,
            document=data,
            filename=f"state_{time.strftime('%m%d_%H%M')}.json",
            caption="状态备份（异地容灾）",
        )

    # ─────────── 通知 ───────────

    async def _tell(self, text: str) -> None:
        try:
            await self.tg.send_message(chat_id=C.TG_CHAT_ID, text=text)
        except Exception as e:
            logger.error(f"推送失败: {e}")

    def _tell_nowait(self, text: str) -> None:
        try:
            asyncio.get_running_loop().create_task(self._tell(text))
        except RuntimeError:
            logger.error(f"无事件循环，无法推送: {text}")
