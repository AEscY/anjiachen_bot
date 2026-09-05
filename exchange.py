"""
交易所封装。

两个实例，这是必须的：
    ex   —— 交易用，可为模拟盘
    pub  —— 行情用，永远真实，且【不带任何密钥】

为什么不能共用一个：ccxt 的 set_sandbox_mode(True) 会整体切换
urls，无法保证 fetch_ticker 仍返回真实市场数据。用模拟行情做
决策，等于在假数据上跑真钱。

为什么 pub 不能带密钥：
    公开行情接口本就不需要认证。带上密钥后，交易所会去校验它 ——
    一旦密钥与当前环境不匹配（如模拟盘 key 请求真实域名），
    连最基础的取价都会返回 50101，整个机器人空转。
    这是真实踩过的坑。

错误分两类：
    fatal   —— 认证/权限错误，重试一万次也不会好，必须人改配置
    transient —— 网络/限频/超时，退避重试即可

    混为一谈的后果：配置错误被当成网络抖动，每 10 秒刷一行
    WARNING，永不停止，而 Telegram 是唯一通知渠道，
    用户根本不知道机器人其实什么都没做。
"""
import logging

import ccxt

import config as C

logger = logging.getLogger(__name__)

# 永久错误：改配置才能好
FATAL = (ccxt.AuthenticationError, ccxt.PermissionDenied)

FATAL_HINT = """
交易所认证失败（{what}）

这不是网络问题，重试不会恢复。

  {msg}

可能原因与处理：
  · API Key 与账户环境不匹配
    模拟盘 key 只能请求模拟盘，实盘 key 只能请求实盘。
    当前 SANDBOX = {sandbox}
  · Key 被删除 / 权限不足 / IP 白名单未包含本服务

处理：
  A. 跑模拟盘 → 去交易所【模拟交易】页面单独申请 demo key
  B. 跑实盘   → 环境变量改 OKX_SANDBOX=false，并填实盘 key

修正后重新部署。在修正前，机器人不会进行任何交易。
"""


def _make(sandbox: bool, with_auth: bool):
    cfg = {
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
    if with_auth:
        cfg["apiKey"] = C.API_KEY
        cfg["secret"] = C.SECRET
        cfg["password"] = C.PASSPHRASE
    ex = getattr(ccxt, C.EXCHANGE_ID)(cfg)
    if sandbox:
        ex.set_sandbox_mode(True)
    return ex


class Exchange:
    def __init__(self, on_fatal=None):
        """
        on_fatal: 可选回调 fn(text)，用于把致命错误推给用户。
                  不给就只写日志。
        """
        self._on_fatal = on_fatal
        self.ex = _make(C.SANDBOX, with_auth=True)
        self.pub = _make(False, with_auth=False)   # ← 关键：不带密钥

        # 致命错误状态。一旦置位，所有需认证操作立即返回空值，
        # 不再重复打交易所（否则每 10 秒刷屏且浪费额度）
        self.fatal = None

        self.markets = self._load_markets()
        logger.info(f"交易所 {C.EXCHANGE_ID} 已连接"
                    f"（{'模拟盘' if C.SANDBOX else '实盘'}）")

    # ─────────── 致命错误 ───────────

    def _is_fatal(self, e: Exception) -> bool:
        if isinstance(e, FATAL):
            return True
        # 有些交易所把认证错误包在 ExchangeError 里，靠错误码兜底
        codes = ("50101", "50111", "50113", "50103",
                 "50100", "50102", "50104")
        return any(c in str(e) for c in codes)

    def _raise_fatal(self, what: str, e: Exception) -> None:
        if self.fatal:
            return                      # 已经报过，不重复推送
        msg = str(e)[:200]
        text = FATAL_HINT.format(
            what=what, msg=msg,
            sandbox="true（模拟盘）" if C.SANDBOX else "false（实盘）")
        self.fatal = text
        logger.error("=" * 50)
        logger.error(f"致命错误：{what} —— 机器人将停止交易")
        logger.error(f"  {msg}")
        logger.error("=" * 50)
        if self._on_fatal:
            try:
                self._on_fatal(text)
            except Exception as ex:
                logger.error(f"推送致命错误失败: {ex}")

    # ─────────── 数据 ───────────

    def _load_markets(self):
        try:
            return self.ex.load_markets()
        except Exception as e:
            if self._is_fatal(e):
                self._raise_fatal("加载交易规则", e)
            else:
                logger.warning(f"加载交易规则失败: {e}")
            return {}

    # ─────────── 精度 ───────────

    def round_qty(self, sym: str, qty: float) -> float:
        if qty <= 0:
            return 0.0
        return float(self.ex.amount_to_precision(sym, qty))

    def round_price(self, sym: str, price: float) -> float:
        return float(self.ex.price_to_precision(sym, price))

    def min_amount(self, sym: str) -> float:
        m = self.markets.get(sym) or {}
        lo = (m.get("limits") or {}).get("amount") or {}
        return float(lo.get("min") or 0.0)

    # ─────────── 行情（真实、无需认证）───────────

    def price(self, sym: str) -> float:
        """现价。失败返回 0（调用方必须检查，不能拿 0 去做决策）。"""
        try:
            t = self.pub.fetch_ticker(sym)
            return float(t.get("last") or 0.0)
        except Exception as e:
            logger.warning(f"取价失败 {sym}: {e}")
            return 0.0

    # ─────────── 账户 ───────────

    def balances(self) -> tuple[float, float, dict]:
        """
        返回 (可用 USDT, 冻结 USDT, {币: 可用数量})

        为什么必须返回冻结部分：

            ccxt 的 free 只含【未冻结】资金。挂出限价买单后，
            USDT 从 free 移到 used —— 钱还在你账户里，
            只是暂时锁定在订单里。

            如果权益只算 free + 持仓，网格一挂单，
            权益就"蒸发"掉全部冻结金额。真实部署日志：

                日基准 7256.38U（可用 4805.53 + 1 ETH 2450.85）
                挂 4 档买单，每档 960.91U，冻结 3843.64U
                权益 → (4805.53-3843.64) + 2450.85 = 3412.74U
                日亏损 = (7256.38-3412.74)/7256.38 = 52.97%
                → 触发日亏损熔断，暂停 1 小时
                → 订单还挂着，1 小时后重算，仍是 52.97%
                → 再次暂停 → 永久停摆

            即：网格越正常工作，越容易把自己熔断。

        所以权益必须是 free + used + 持仓市值。
        而【可动用】金额仍然是 free —— 冻结的钱不能重复挂单。
        """
        if self.fatal:
            return 0.0, 0.0, {}
        try:
            b = self.ex.fetch_balance()
        except Exception as e:
            if self._is_fatal(e):
                self._raise_fatal("读取账户余额", e)
            else:
                logger.warning(f"取余额失败: {e}")
            return 0.0, 0.0, {}

        u = b.get("USDT") or {}
        free_usdt = float(u.get("free") or 0.0)
        used_usdt = float(u.get("used") or 0.0)
        total_usdt = float(u.get("total") or 0.0)

        # 部分交易所不返回 used，用 total - free 兜底
        if used_usdt <= 0 and total_usdt > free_usdt:
            used_usdt = total_usdt - free_usdt

        coins = {}
        for k, v in (b.get("free") or {}).items():
            if k == "USDT" or not isinstance(v, (int, float)):
                continue
            if float(v) > 0:
                coins[k] = float(v)
        return free_usdt, used_usdt, coins

    # ─────────── 订单 ───────────

    def open_orders(self, sym: str) -> list[dict]:
        if self.fatal:
            return []
        try:
            return self.ex.fetch_open_orders(sym)
        except Exception as e:
            if self._is_fatal(e):
                self._raise_fatal("读取挂单", e)
            else:
                logger.warning(f"取挂单失败 {sym}: {e}")
            return []

    def fetch_order(self, oid: str, sym: str) -> dict | None:
        if self.fatal:
            return None
        try:
            return self.ex.fetch_order(oid, sym)
        except Exception as e:
            if self._is_fatal(e):
                self._raise_fatal("查询订单", e)
            else:
                logger.warning(f"查单失败 {sym} {oid}: {e}")
            return None

    def limit_buy(self, sym: str, qty: float, price: float) -> str | None:
        return self._place("buy", sym, qty, price)

    def limit_sell(self, sym: str, qty: float, price: float) -> str | None:
        return self._place("sell", sym, qty, price)

    def _place(self, side: str, sym: str, qty: float, price: float):
        if qty <= 0 or price <= 0:
            return None
        if self.fatal:
            return None
        try:
            fn = self.ex.create_limit_buy_order if side == "buy" \
                else self.ex.create_limit_sell_order
            o = fn(sym, qty, price)
            return o.get("id")
        except Exception as e:
            if self._is_fatal(e):
                self._raise_fatal(f"下单（{side}）", e)
            else:
                logger.error(f"下单失败 {side} {sym} {qty}@{price}: {e}")
            return None

    def cancel(self, oid: str, sym: str) -> None:
        if self.fatal:
            return
        try:
            self.ex.cancel_order(oid, sym)
        except Exception as e:
            if self._is_fatal(e):
                self._raise_fatal("撤单", e)
            else:
                logger.warning(f"撤单失败 {sym} {oid}: {e}")

    def market_sell(self, sym: str, qty: float) -> str | None:
        """市价卖出，用于清仓。"""
        if qty <= 0:
            return None
        if self.fatal:
            return None
        try:
            o = self.ex.create_market_sell_order(sym, qty)
            return o.get("id")
        except Exception as e:
            if self._is_fatal(e):
                self._raise_fatal("市价卖出", e)
            else:
                logger.error(f"市价卖出失败 {sym} {qty}: {e}")
            return None
