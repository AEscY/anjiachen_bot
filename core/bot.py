"""
UltimateBot v14.1 - 精简现货低吸高卖引擎 + 市场自适应
新增：轻量级自适应调节器（根据波动/趋势动态调整阈值、仓位、止盈止损）
"""
import asyncio
import aiohttp
import json
import time
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import settings, logger
from core.indicators import TechnicalEngine
from core.risk import RiskManager
from core.signals import SignalEngine, ScoreEngine
from core.watchdog import Watchdog
from core.trend import TrendWatcher
from core.guardian import PriceGuard, Retirement
from core.reporter import DailyReporter
from core.ws_manager import WSDataManager
from storage import (
    init_db, load_config, save_config, load_trades, save_trade,
    save_trade_detail, get_recent_performance, get_today_trades,
    export_db_to_json, save_runtime_state, load_runtime_state,
    get_total_fees, get_total_net_profit, now_parts
)
# 上面是"导入具体函数"，但模块本身也要导入：
# 备份模块需要读 storage.DB_FILE（延迟读取，因为测试会改写它），
# 只导入函数名是拿不到模块对象的，会导致 NameError。
import storage

CST = timezone(timedelta(hours=8))

# 允许的交易周期（原实现接受任意字符串，拼错会静默拉不到 K 线）
VALID_TIMEFRAMES = {
    '1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '1w'
}

# ── 面板按钮 → 交互式输入规格 ──
# 原实现只提示 "请输入 amount 的新值，例如 /amount 1.2"，
# 但一没有 /amount 这个命令，二 handle_text_input 是空的(pass)，
# 用户照着提示输入后毫无反应 —— 这是面板按钮"点了没用"的根因。
#
# 这里声明每个按钮要收集什么，由 handle_text_input 统一消费：
#   ("param",  参数名, 提示语)   → 走 /set 的校验与换算
#   ("timeframe", None, 提示语) → 走 /settf
#   ("add_symbol"/"del_symbol", None, 提示语) → 走 增删币种
# ── 快捷值：点一下就设好，不用手输 ──
# 常用就那几个值，每次手打既慢又容易弄错单位。
QUICK_VALUES = {
    "tp_pct":           ["1", "1.5", "2", "3", "5", "8"],
    "sl_pct":           ["0.5", "1", "2", "3", "5"],
    "trailing_sl_pct":  ["0.3", "0.5", "1", "2"],
    "trailing_tp_pct":  ["0.2", "0.3", "0.5", "1"],
    "single_order_pct": ["1", "2", "5", "10"],
    "reserve_bottom":   ["1", "5", "10"],
    "max_daily_trades": ["5", "10", "20", "50"],
    "timeframe":        ["5m", "15m", "1h", "4h"],
}

MENU_INPUT_SPEC = {
    "menu_set_tp":      ("param", "tp_pct",           "止盈百分比"),
    "menu_set_sl":      ("param", "sl_pct",           "止损百分比"),
    "menu_set_tsl":     ("param", "trailing_sl_pct",  "移动止损百分比"),
    "menu_set_tmpt":    ("param", "trailing_tp_pct",  "移动止盈百分比"),
    "menu_set_amount":  ("param", "single_order_pct", "单笔占总资金百分比"),
    "menu_set_reserve": ("param", "reserve_bottom",   "保留底线金额"),
    "menu_set_trades":  ("param", "max_daily_trades", "每日最多交易次数"),
    "menu_set_tf":      ("timeframe", None,           "K 线周期"),
    "menu_add_symbol":  ("add_symbol", None,          "要添加的币种"),
    "menu_del_symbol":  ("del_symbol", None,          "要删除的币种"),
}
# 监控币种上限：每个币种都会占用 WebSocket 订阅与 REST 轮询配额
MAX_SYMBOLS = 20

# ==================== 实时数据引擎（简化为仅恐惧贪婪） ====================
class RealDataEngine:
    def __init__(self, exchange, ws):
        self.exchange = exchange
        self.ws = ws
        self._fear_greed_cache = {"value": 50, "classification": "Neutral", "timestamp": 0}
        self._cache_ttl = 300
        self._session = None

    async def _get_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def get_fear_greed_index(self):
        now = time.time()
        if now - self._fear_greed_cache["timestamp"] < self._cache_ttl:
            return self._fear_greed_cache
        try:
            session = await self._get_session()
            async with session.get("https://api.alternative.me/fng/?limit=1", timeout=5) as resp:
                data = await resp.json()
                if data.get("data"):
                    item = data["data"][0]
                    self._fear_greed_cache = {
                        "value": int(item["value"]),
                        "classification": item["value_classification"],
                        "timestamp": now
                    }
        except Exception as e:
            logger.warning(f"恐惧贪婪获取失败: {e}")
        return self._fear_greed_cache

# ==================== 核心机器人 ====================
class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.ws = WSDataManager(exchange)
        self.tech = TechnicalEngine(exchange)
        self.real_data = RealDataEngine(exchange, self.ws)

        # 网格引擎与执行层（网格模式下使用）
        from core.grid import GridEngine
        from core.execution import OrderExecutor
        self.grid = GridEngine(self, exchange)
        self.executor = OrderExecutor(exchange, self.grid, self)
        self._last_atr_pct = 0.0
        # 网格全局锁：保护配置与运行时状态的写入
        self.lock = asyncio.Lock()
        # 按币种串行锁：_auto_trade_monitor(买) 与 _trailing_monitor(卖) 并发读写同一份
        # position_lots 账本，下单的网络 I/O 之间存在竞态窗口，可能导致超卖或
        # ValueError 触发永久暂停。按币种加锁保证同币种操作串行，不同币种仍并行。
        self._symbol_locks: dict = {}
        self._symbol_locks_guard = asyncio.Lock()

        # 基础配置：所有可调参数统一从 params 注册表取默认值。
        # 新增参数只需在 core/params.py 加一行，无需改动此处。
        from core.params import defaults as _param_defaults
        for _k, _v in _param_defaults().items():
            setattr(self, _k, _v)

        # 非参数的运行时状态（不进注册表）
        self.symbols = [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]
        self.timeframe = "5m"
        self.auto_trade_enabled = False
        self.orderbook_filter = True
        self.single_order_usdt = 1.0        # 单次模式单笔额度（U）
        self.max_single_order_pct = 0.10    # 单笔硬上限 10%

        # 运行时状态
        self.is_running = True
        self.trades = []
        self.entries = {}
        self.position_lots = {}          # FIFO账本（单次模式）
        self._ready = False               # 健康检查用：初始化完成才算就绪
        self._last_alive = None           # 主循环心跳时间戳

        # 启动对账：比对交易所真实余额与本地账本，
        # 防止数据库丢失后机器人"以为空仓"而重复买入、止损失效
        from core.reconcile import Reconciler
        self.reconciler = Reconciler(self)

        # 自动备份：定时导出数据库，并在数据丢失时自动从备份恢复
        from core.backup import BackupManager
        self.backup_mgr = BackupManager(
            lambda: storage.DB_FILE,
            uploader=self._upload_backup,   # 推到 Telegram，避免本地盘一锅端
            upload_every=4,                 # 6h×4 = 每天推一次
        )
        self._stop_backup = None
        self.position_counts = {}
        self._trailing_high = {}
        self.entry_details = {}
        self.last_reset_day = datetime.now(CST).date().isoformat()

        # 市场自适应（按波动/趋势动态调阈值与仓位）
        # 这些是随行情推导出的运行中状态，不是用户配置，故不进参数注册表
        #
        # ⚠️ 必须按币种隔离：BTC 与 DOGE 的波动特性完全不同，
        #    共用一个全局变量会让后处理的币种沿用先处理币种的状态。
        #    实测四个币种会全部显示同一状态（见 test_adaptive.py）。
        self._adaptive = {}          # {sym: {state,tp_factor,sl_factor,offset,amount_factor,ts}}
        # 下面是"最后一次更新"的镜像，仅供外部读取兼容（signals.py / 老代码）
        self._adaptive_state = 'neutral'
        self._adaptive_tp_factor = 1.0
        self._adaptive_sl_factor = 1.0
        self._adaptive_score_offset = 0
        self._adaptive_amount_factor = 1.0
        self._adaptive_update_time = 0.0

        # 缓存（余额与指标的本地缓存，避免频繁打交易所接口）
        self._cached_balances = {}
        self._cached_usdt_free = 0.0
        self._balance_cache_time = 0
        self._balance_cache_ttl = 15
        self._tech_cache = {}
        self._tech_cache_time = {}
        self._tech_cache_ttl = 30
        self._price_history = {}

        # 费率
        self.taker_fee = settings.TAKER_FEE
        self.maker_fee = settings.MAKER_FEE
        self.min_profit_margin = settings.MIN_PROFIT_MARGIN
        self.breakeven_pct = (self.taker_fee * 2) + self.min_profit_margin

        # 风控状态统一由 RiskManager 承载，此处保留向后兼容的属性代理
        self.risk = RiskManager(self, alert=self._alert)

        # Telegram 权限
        raw = settings.ALLOWED_USERS
        self.allowed = ({int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}
                        if raw else set())
        self.env_tag = "🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"
        self.coin_configs = {}
        # 启动时被判定无效的交易对 {sym: reason}，隔离而非删除
        self._invalid_symbols = {}

        # 持久化失败告警节流（每轮都保存，失败时不能每轮都告警）
        self._last_persist_alert = 0.0
        self._persist_fail_count = 0

        # Telegram 应用
        self.tg_app = None
        # Telegram 断连自保（原实现完全没有这个状态，
        # 于是 Telegram 是单点：它一挂，用户就彻底失联且不知情）
        self._tg_down_since = None      # 断连起始时间戳
        self._tg_fatal_reported = False # 致命错误是否已告警过

        # 按参数注册表补齐默认值（新增参数无需再改 __init__）
        self._ensure_param_defaults()

        # 行为看门狗：纯观测，只报告不改参数
        self.watchdog = Watchdog(self, alert=self._alert, logger=logger)
        # 趋势退化检测：看"是不是在持续变差"，而非"有没有超阈值"
        self.trend = TrendWatcher(self, logger=logger)
        # 价格守卫：突变拦截（下单前）+ 滑点检测（成交后）
        self.price_guard = PriceGuard(self, alert=self._alert, logger=logger)
        # 退役线：全局累计亏损底线，触发后不自动恢复
        self.retirement = Retirement(self, alert=self._alert, logger=logger)
        # 日报：兼作心跳证明 —— 收到即说明机器人活着
        self.reporter = DailyReporter(self, alert=self._alert, logger=logger)
        if settings.TG_BOT_TOKEN:
            self._init_telegram()

    # ---------- 辅助函数 ----------
    def _auth(self, update: Update):
        if not self.allowed:
            return settings.IS_SANDBOX
        return update.effective_user.id in self.allowed

    async def _sym_lock(self, sym: str) -> asyncio.Lock:
        """获取（必要时创建）某币种的串行锁。"""
        if sym not in self._symbol_locks:
            async with self._symbol_locks_guard:
                if sym not in self._symbol_locks:
                    self._symbol_locks[sym] = asyncio.Lock()
        return self._symbol_locks[sym]

    def _ensure_param_defaults(self):
        """
        给所有注册参数补默认值兜底。

        params.py 每新增一个参数，bot 必须自动获得同名属性。
        原实现靠 __init__ 里一处集中赋值，漏写一个就会在运行到那行时
        抛 AttributeError —— 而且是【只在特定行情分支下】才触发，
        测试很难覆盖。改为按注册表自动补齐，永不遗漏。
        """
        from core.params import PARAMS
        missing = []
        for key, spec in PARAMS.items():
            if not hasattr(self, key):
                setattr(self, key, spec.default)
                missing.append(key)
        if missing:
            logger.info(f"ℹ️ 已补齐 {len(missing)} 个参数默认值")

    async def _sanitize_symbols(self):
        """
        启动时清洗监控列表。

        场景：上次手滑加了个不存在的交易对，或者交易所把某个币下线了。
        若不清理，WebSocket 订阅会【整体失败】—— 一个坏币种拖垮所有币种。

        采取"隔离而非删除"：把无效交易对移到 self._invalid_symbols，
        保留在配置里（万一交易所只是临时下线），但不参与订阅与交易。
        """
        bad = []
        for sym in list(self.symbols):
            ok, reason = self._validate_symbol(sym)
            if not ok:
                bad.append((sym, reason))
                continue
            exists, why = await self._market_exists(sym)
            if not exists:
                bad.append((sym, why))
        if not bad:
            return []
        self._invalid_symbols = {s: r for s, r in bad}
        for sym, _ in bad:
            if sym in self.symbols:
                self.symbols.remove(sym)
        for sym, reason in bad:
            logger.warning(f"⚠️ 已隔离无效交易对 {sym}: {reason}")
        logger.warning(f"⚠️ 共隔离 {len(bad)} 个交易对，其余币种不受影响")
        return bad

    def _get_coin_param(self, sym, key, default):
        return self.coin_configs.get(sym, {}).get(key, default)

    def _position_lots_for(self, sym):
        return self.position_lots.setdefault(sym, [])

    @staticmethod
    def _lot_cost(lot):
        """
        读取单条持仓记录的成本（USDT 计价）。

        ⚠️ 与 _lot_amount 同理，键名历史上出现过两种：
            买入写入 'cost'，v14 的 /import 误写 'cost_usdt'。

        v17 我只统一了数量读取，漏了成本 —— _bot_position_cost
        只读 'cost'，旧记录返回 0，导致 _weighted_entry 均价算成 0，
        止盈止损判断全部失真。

        凡是要读持仓成本的地方，一律调用本函数。
        """
        try:
            v = lot.get('cost')
            if v is None:
                v = lot.get('cost_usdt')
            return float(v or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _lot_amount(lot):
        """
        读取单条持仓记录的数量。

        ⚠️ 必须统一走这里。键名历史上出现过两种：
            买入写入 'amount'，v14 的 /import 误写 'qty'。

        v16 我只修了 _bot_position_amount，漏了
        _consume_position_lots —— 结果能读到数量、能下单，
        但扣减账本时预检查失败抛 ValueError。
        后果比原问题更糟：
            原问题：没卖（账本与交易所都还在）
            新问题：真卖了，但账本没扣 → 对账不一致 → 阻塞

        凡是要读持仓数量的地方，一律调用本函数。
        """
        try:
            v = lot.get('amount')
            if v is None:
                v = lot.get('qty')
            return float(v or 0)
        except Exception:
            return 0.0

    def _bot_position_amount(self, sym):
        """
        读取本地账本持仓量。

        ⚠️ 键名兼容：买入写入 'amount'，但 v14 的 /import 误写 'qty'。
        结果本函数恒返回 0，导致 /panic 静默跳过 ——
        不下单、不报错，只回复"全平完成"。
        用户以为平了，交易所持仓纹丝不动。

        此处同时兼容两种键名，数据库中已有的旧记录也能正常处理。
        """
        return sum(self._lot_amount(l) for l in self.position_lots.get(sym, []))

    def _bot_position_cost(self, sym):
        return sum(self._lot_cost(l) for l in self.position_lots.get(sym, []))

    def _weighted_entry(self, sym):
        lots = self.position_lots.get(sym, [])
        amount = sum(self._lot_amount(l) for l in lots)
        cost = sum(self._lot_cost(l) for l in lots)
        return cost / amount if amount > 0 else 0.0

    def _append_position_lot(self, sym, amount, price, cost, fee=0.0,
                             currency='', fee_usdt=None):
        """
        记一笔买入。

        fee         原始手续费（数量随币种而定，可能是币数也可能是 USDT）
        fee_usdt    折算成 USDT 后可安全参与盈亏计算的手续费
                    · base 币扣费 → 0（已通过减少 amount 体现，不重复计）
                    · quote 币扣费 → 面值
                    · 旧数据无此字段 → None，回退按币种判断（见下）
        """
        if fee_usdt is None:
            # 兼容旧账本：按币种推断
            base = sym.split('/')[0] if '/' in sym else ''
            fee_usdt = 0.0 if currency == base else float(fee)
        self._position_lots_for(sym).append({
            'amount': float(amount), 'price': float(price), 'cost': float(cost),
            'fee': float(fee), 'fee_currency': currency,
            'fee_usdt': float(fee_usdt), 'time': time.time()
        })
        self.entries[sym] = self._weighted_entry(sym)
        self.position_counts[sym] = len(self.position_lots[sym])
        self._trailing_high[sym] = max(self._trailing_high.get(sym, self.entries[sym]), price)

    def _consume_position_lots(self, sym, amount, exit_price, exit_revenue, sell_fee=0.0):
        """
        FIFO 消耗账本。

        ⚠️ 原子性修复：原实现边遍历边扣减，走到最后才发现数量不够，
        此时 lot 已被消耗并 pop，再抛 ValueError —— 调用方 catch 后只
        continue，不回滚，导致这部分持仓从账本永久消失，
        而交易所里币还在，下次启动对账必然判「不一致」并暂停交易。

        现改为【先预检查、后扣减】：不够就原样抛出，账本一个字节都不动。
        """
        remaining = float(amount)

        # ── 预检查：账本是否够扣（在修改任何状态之前）──
        available = sum(self._lot_amount(l)
                        for l in self.position_lots.get(sym, []))
        # 容差对齐下面的循环判定，避免 1e-12 量级的浮点噪声误报
        if available + 1e-8 < remaining:
            raise ValueError(
                f'{sym} 卖出数量超过账本: 需要{remaining:.8f}, '
                f'可用{available:.8f}（账本未做任何修改）')

        realized_cost = 0.0
        realized_fee_buy = 0.0
        while remaining > 1e-12 and self.position_lots.get(sym):
            lot = self.position_lots[sym][0]
            lot_amt = self._lot_amount(lot)
            take = min(remaining, lot_amt)
            ratio = take / lot_amt if lot_amt else 0
            realized_cost += float(lot.get('cost', 0)) * ratio
            # 用折算后的 fee_usdt，而不是原始 fee（原始值可能是币数）
            realized_fee_buy += float(lot.get('fee_usdt', 0)) * ratio
            lot['amount'] = lot_amt - take
            # 若原记录用的是 qty（v14 旧数据），同步扣减，
            # 否则 amount 归零但 qty 仍是原值，读取端会误判
            if 'qty' in lot:
                lot['qty'] = lot_amt - take
            lot['cost'] = float(lot.get('cost', 0)) * (1 - ratio)
            lot['fee'] = float(lot.get('fee', 0)) * (1 - ratio)
            lot['fee_usdt'] = float(lot.get('fee_usdt', 0)) * (1 - ratio)
            remaining -= take
            if lot['amount'] <= 1e-12:
                self.position_lots[sym].pop(0)
        if remaining > 1e-8:
            # 预检查通过后这里理论上不会触发；若触发说明存在并发修改，
            # 此时账本已被部分消耗，属于严重不一致，必须告警而非静默
            logger.error(
                f"🚨 {sym} 账本并发修改导致超卖 {remaining:.8f}，"
                f"请立即 /reconcile 核对持仓")
            raise ValueError(f'{sym} 卖出数量超过账本: remaining={remaining}')
        self.position_counts[sym] = len(self.position_lots.get(sym, []))
        if self.position_lots.get(sym):
            self.entries[sym] = self._weighted_entry(sym)
        else:
            self.position_lots.pop(sym, None)
            self.entries.pop(sym, None)
            self.entry_details.pop(sym, None)
            # 原实现写入 0 而非删除：下次 _trailing_high.get(sym, entry) 会拿到 0，
            # 导致 'p <= high*(1-ttp)' 恒为 False，移动止盈/止损静默失效
            self._trailing_high.pop(sym, None)
        net_pnl = float(exit_revenue) - realized_cost - realized_fee_buy - float(sell_fee or 0)
        return net_pnl, realized_cost, realized_fee_buy

    async def _refresh_balance_cache(self, force=False):
        now = time.time()
        if force or (now - self._balance_cache_time > self._balance_cache_ttl):
            bal = await self.exchange.fetch_balance()
            self._cached_balances = bal
            self._cached_usdt_free = float(bal.get('USDT', {}).get('free', 0))
            self._balance_cache_time = now
        return self._cached_usdt_free

    async def _round_amount_by_precision(self, symbol, amount):
        return await self.exchange._prepare_amount(symbol, amount)

    def _total_equity_usdt(self) -> float:
        """账户总权益（USDT 现货 + 各币种按现价折算）。_tickers 为空时退化为可用 USDT。"""
        total = float(self._cached_usdt_free or 0)
        for coin, val in self._cached_balances.items():
            if coin == 'USDT' or not isinstance(val, dict):
                continue
            ticker = self.ws.get_ticker(coin + "/USDT")
            if ticker:
                total += float(val.get('free', 0)) * float(ticker.get('last', 0))
        return total

    def _effective_equity_usdt(self) -> float:
        """
        用于仓位计算的有效权益 = min(实际权益, equity_cap_usdt)。

        为什么要这一层：
          仓位算法按权益【比例】缩放。OKX 模拟盘给 10 万 U 虚拟资金，
          而实盘只有 9U —— 不加上限的话单笔金额会差一万倍，
          模拟盘永远不会撞上交易所的最小交易额/最小交易量。
          结果是：模拟盘跑得很顺，一切到实盘就"一单都不下"。

          加了上限，模拟盘就能精确复现目标资金规模下的行为，
          验证出的参数可直接平移到实盘。

        实盘同样有用：大账户只拿一部分资金试水。

        注意：余额【显示】仍用真实值，只影响仓位计算。
        """
        real = self._total_equity_usdt()
        cap = float(getattr(self, 'equity_cap_usdt', 0) or 0)
        if cap <= 0:
            return real
        if real > cap:
            return cap
        return real

    def _calculate_dynamic_amount(self, base_amount=1.0):
        """
        单笔额度 = max(下限, 总权益 × 单笔占比)，并受单笔占比上限约束。
        原实现是阶梯常量：资金 100U 与 100000U 都只买 2U，大资金利用率极低。
        """
        total_balance = self._effective_equity_usdt()
        if total_balance < 10:
            return max(0.1, base_amount * 0.3)
        proportional = total_balance * self.single_order_pct
        floor = base_amount * 0.5
        cap = total_balance * self.max_single_order_pct
        return max(floor, min(proportional, cap))

    async def _allocation_used_usdt(self):
        used = 0.0
        for sym in self.symbols:
            used += self._bot_position_cost(sym)
        return max(0.0, used)

    async def _can_allocate(self, additional_usdt):
        balance = self._effective_equity_usdt()
        if balance <= 0:
            return False
        used = await self._allocation_used_usdt()
        max_alloc = balance * self.max_total_allocated_pct
        return used + additional_usdt <= max_alloc + 1e-9

    async def _check_risk_limits(self):
        today = datetime.now(CST).date().isoformat()
        if today != self.last_reset_day:
            self._today_loss_pct = 0.0
            self._today_loss_usdt = 0.0
            self._daily_start_equity = 0.0
            self._consecutive_losses = 0
            self.last_reset_day = today

        # ---- 1) 回撤熔断：此前只在 _risk_monitor_task 里算了标志位，从不阻止开仓 ----
        if not self._drawdown_safe_flag:
            if not self._drawdown_alerted:
                await self._alert(
                    f"⛔ 回撤达上限 {self.max_drawdown_pct*100:.0f}%，禁止新开仓（平仓逻辑不受影响）",
                    "critical",
                )
                self._drawdown_alerted = True
            return False
        self._drawdown_alerted = False

        # ---- 2) 连续亏损熔断：_last_pause_time 此前从未写入，导致条件恒真、熔断形同虚设 ----
        if self._consecutive_losses >= self.max_consecutive_losses:
            if self._last_pause_time <= 0:
                # 首次触发：记录时间并进入冷静期
                self._last_pause_time = time.time()
                self._is_paused = True
                await self._alert(
                    f"⛔ 连续亏损 {self._consecutive_losses} 笔，进入 {self.consecutive_loss_cooldown//60} 分钟冷静期",
                    "critical",
                )
                return False
            elapsed = time.time() - self._last_pause_time
            if elapsed >= self.consecutive_loss_cooldown:
                # 冷静期结束：复位
                self._consecutive_losses = 0
                self._last_pause_time = 0
                self._is_paused = False
                await self._alert("✅ 连续亏损冷静期结束，恢复交易", "info")
            else:
                remain = int(self.consecutive_loss_cooldown - elapsed)
                if remain % 600 < 10:   # 避免每 10s 刷屏
                    await self._alert(f"⏳ 冷静期剩余 {remain//60} 分钟", "warning")
                return False

        # ---- 3) 日内亏损熔断 ----
        if self._today_loss_pct >= self.max_daily_loss_pct:
            if not self._is_paused:
                await self._alert(f"⛔ 日亏损达 {self._today_loss_pct*100:.1f}%，暂停交易", "critical")
                self._is_paused = True
            return False

        return True

    # ---------- Telegram 错误分级 ----------

    # 永久性错误：重试一万次也不会成功，继续重试只是浪费资源，
    # 更糟的是让用户以为"系统在自愈"，实际永远连不上。
    _TG_FATAL_PATTERNS = (
        "invalidtoken", "unauthorized",      # token 失效/被 revoke
        "forbidden", "bot was blocked",      # 用户拉黑机器人
        "chat not found",                    # TG_CHAT_ID 填错
    )

    @classmethod
    def _classify_tg_error(cls, e):
        """
        判断 Telegram 错误是【临时性】还是【永久性】。

        为什么必须区分（这是真实事故场景）：
          用户 revoke token 后，机器人每 60 秒重试一次，
          永远失败、永不停止。而 Telegram 是唯一的通知渠道，
          于是用户【完全不知情】，交易却仍在跑 ——
          /panic 发不出去，持仓彻底失控。

        临时性（可重试）：
          NetworkError / Bad Gateway / TimedOut / Conflict
        永久性（不可重试，必须立刻告知）：
          InvalidToken / Unauthorized / Forbidden / chat not found
        """
        name = type(e).__name__.lower()
        msg = str(e).lower()
        blob = name + " " + msg
        for pat in cls._TG_FATAL_PATTERNS:
            if pat in blob:
                return "fatal"
        if "conflict" in blob:
            return "conflict"
        return "transient"

    def _note_tg_down(self):
        """记录 Telegram 断连起点，用于计算持续时长"""
        if self._tg_down_since is None:
            self._tg_down_since = time.time()

    def _note_tg_up(self):
        """Telegram 恢复，清除断连状态并返回持续秒数"""
        down = 0.0
        if self._tg_down_since is not None:
            down = time.time() - self._tg_down_since
        self._tg_down_since = None
        self._tg_fatal_reported = False
        return down

    async def _alert(self, message, level="warning"):
        emoji = {"info":"ℹ️","warning":"⚠️","critical":"🚨"}
        # 无论推送成功与否都落日志，避免告警在推送失败时被完全吞掉
        log = logger.warning if level != "info" else logger.info
        log(f"[{level}] {message}")
        if settings.TG_CHAT_ID and self.tg_app:
            try:
                await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"{emoji.get(level,'⚠️')} **系统告警**\n{message}", parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"告警推送失败: {e}")

    # ---------- 加载与保存 ----------
    async def load_and_init(self):
        # 恢复必须在读库之前：若检测到数据丢失，先用最近备份回填
        try:
            restored, note = await self.backup_mgr.restore_if_needed()
            if restored:
                logger.warning(f"♻️ {note}")
                self._pending_restore_note = note
            elif note:
                logger.info(f"ℹ️ {note}")
        except Exception as e:
            logger.warning(f"备份恢复检查失败，继续启动: {e}")

        await init_db()
        cfg = await load_config()

        # 注册表参数：按声明的类型与范围校验后再套用，
        # 损坏的配置项回退默认值，不会像从前那样导致整份配置丢失
        from core.params import PARAMS, parse
        for key, val in cfg.items():
            if key not in PARAMS:
                continue
            spec = PARAMS[key]
            if isinstance(val, (bool, int, float, str)) and type(val) is spec.ptype:
                setattr(self, key, val)
                continue
            parsed, err = parse(key, val)
            if err:
                logger.warning(f"配置项 {key}={val!r} 无效({err})，使用默认 "
                               f"{spec.default}")
                setattr(self, key, spec.default)
            else:
                setattr(self, key, parsed)

        # 非注册表项
        for key in ('timeframe', 'auto_trade_enabled', 'orderbook_filter',
                    'single_order_usdt'):
            if key in cfg:
                setattr(self, key, cfg[key])
        self.symbols = cfg.get('symbols') or [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]

        # 启动自检：隔离无效交易对，防止一个坏币种拖垮整个 WebSocket 订阅
        try:
            bad = await self._sanitize_symbols()
            if bad:
                self._pending_symbol_warning = (
                    f"⚠️ 已隔离 {len(bad)} 个无效交易对（其余币种不受影响）：\n"
                    + "\n".join(f"  · {s} — {r}" for s, r in bad[:5])
                    + (f"\n  ...等 {len(bad)} 个" if len(bad) > 5 else ""))
        except Exception as e:
            logger.warning(f"交易对自检跳过: {e}")
        self.coin_configs = json.loads(cfg.get('coin_configs', '{}')) if isinstance(cfg.get('coin_configs'), str) else cfg.get('coin_configs', {})
        self.trades = await load_trades()

        state = await load_runtime_state()
        if state:
            self.position_counts = state.get('position_counts', {})
            self.entries = state.get('entries', {})
            self._trailing_high = state.get('trailing_high', {})
            self.position_lots = state.get('position_lots', {})
            # 恢复巡检状态：否则每次重启都会重新计时，
            # "连续 N 小时未开仓"这类告警将永远触发不了
            try:
                self.watchdog.from_dict(state.get('watchdog') or {})
                self.trend.from_dict(state.get('trend') or {})
                # 退役线必须恢复：否则 Render 重启后清零，
                # 退役线形同虚设（这是它最容易失效的地方）
                self.retirement.from_dict(state.get('retirement') or {})
                self.reporter.from_dict(state.get('reporter') or {})
                # 价格守卫的"暂停到何时"需要恢复，但价格历史不恢复 ——
                # 重启后行情可能已变，用旧数据判定反而危险
                self.price_guard.from_dict(state.get('price_guard') or {})
            except Exception as e:
                logger.debug(f"巡检状态恢复跳过: {e}")
            self.entry_details = state.get('entry_details', {})
            # 风控状态交由 RiskManager 恢复
            self.risk.from_dict(state.get('risk', {}))
            # 网格状态
            grids = state.get('grid_states', {})
            if isinstance(grids, dict):
                from core.grid import GridState
                for sym, gd in grids.items():
                    gs = GridState.from_dict(gd)
                    if gs:
                        self.grid.states[sym] = gs
        mode = "网格" if self.grid_enabled else "单次低吸高卖"
        logger.info(f"✅ 启动完成 | 模式: {mode} | 层数 {self.grid_levels} | "
                    f"间距 {self.grid_spacing_pct*100:.2f}% ({self.grid_spacing_mode}) | "
                    f"中枢 {self.grid_anchor_mode}")

        # 启动对账：务必在恢复状态之后、开始交易之前
        await self._startup_reconcile()

        # 从备份恢复的情况要单独告警，提醒用户核对
        note = getattr(self, "_pending_restore_note", None)
        if note:
            await self._alert(f"♻️ **已从备份恢复数据库**\n\n{note}",
                              level="warning")

        # 无效交易对被隔离：必须告诉用户，否则他以为那个币还在监控
        warn = getattr(self, "_pending_symbol_warning", None)
        if warn:
            await self._alert(f"⚠️ **部分交易对已被隔离**\n\n{warn}\n\n"
                              f"剩余 {len(self.symbols)} 个币种正常监控",
                              level="warning")

        # 启动后台定时备份
        self._start_backup_loop()
        self._start_watchdog_loop()
        self._check_mode_conflict()

        # 模式冲突：两个买循环并发会超配，必须让用户知道
        mode_warn = getattr(self, "_pending_mode_warning", None)
        if mode_warn:
            await self._alert(mode_warn, level="critical")

        # 初始化全部完成，健康检查从现在起才报健康
        self._ready = True
        self.mark_alive()
        logger.info("✅ 健康检查已就绪（UptimeRobot 等外部监控可探测真实状态）")

    # ---------- 启动对账 ----------

    async def _startup_reconcile(self):
        """
        启动时比对交易所真实余额与本地账本。

        对不上的币种会被暂停交易并推送告警 —— 宁可不跑，
        也不能在"账本为空但实盘有币"的状态下重复买入。
        """
        try:
            results = await self.reconciler.check_all()
        except Exception as e:
            logger.warning(f"启动对账失败，本次不阻塞: {e}")
            return

        blocking = [r for r in results if r.blocking]
        drift = [r for r in results if r.level == "drift"]

        if not results:
            return

        if drift:
            logger.info(f"🔍 对账轻微偏差 {len(drift)} 项:\n" + self.reconciler.summary())

        if blocking:
            # 区分两种不一致，给出对应的解锁路径。
            # 原实现只说"去人工核对"，但没给工具 ——
            # 在 Render 免费层（临时盘）上数据库会随重启丢失，
            # 于是每次重启都阻塞，形成死锁。
            only_missing_local = all(
                float(getattr(r, "local_qty", 0) or 0) <= 0
                and float(getattr(r, "exchange_qty", 0) or 0) > 0
                for r in blocking)
            if only_missing_local:
                guide = ("本地账本为空，但交易所有真实持仓 ——\n"
                         "多数是数据库随重启丢失（Render 免费层临时盘）。\n\n"
                         "解锁方式：\n"
                         "  `/import`  把交易所持仓导入账本（推荐）\n"
                         "  `/resume`  若确认这些持仓与机器人无关，直接放行")
            else:
                guide = ("本地账本与交易所数量对不上。\n\n"
                         "请先到交易所核对真实情况，然后：\n"
                         "  `/import`  以交易所数据为准，重建本地账本\n"
                         "  `/resume`  若已人工核对无误，解除暂停")
            msg = ("🚨 **启动对账发现持仓不一致**\n\n"
                   + self.reconciler.summary()
                   + "\n\n已暂停上述币种交易。\n\n" + guide)
            await self._alert(msg, level="critical")
            logger.error(f"🚨 对账阻塞 {len(blocking)} 个币种，已暂停交易")
        else:
            ok = sum(1 for r in results if r.level == "ok")
            logger.info(f"✅ 启动对账通过（{ok}/{len(results)} 一致）")

    # ---------- 备份的上传与恢复 ----------

    # ---------- 健康检查 ----------

    def mark_alive(self):
        """主循环每转一圈刷新一次心跳。"""
        self._last_alive = time.time()

    def health_status(self) -> dict:
        """
        给健康检查端点用的真实状态。

        背景：原来的健康检查对任意路径无条件返回 200 "OK"，
        无论机器人内部是否已经卡死。这意味着外部监控（UptimeRobot 等）
        只能证明"端口开着"，证明不了"机器人在工作"。

        现在会报告：进程是否就绪、距上次心跳多久、Telegram 是否连着。
        外部监控据此能真正发现"假活" —— 端口通但主循环已停。
        """
        now = time.time()
        last = getattr(self, "_last_alive", None)
        age = (now - last) if last else None

        tg_up = bool(self.tg_app and getattr(self.tg_app, "running", False)
                     and self.tg_app.updater
                     and getattr(self.tg_app.updater, "running", False))

        # 心跳超过 5 分钟没刷新，判定主循环停滞
        stale = age is not None and age > 300
        healthy = self._ready and not stale and tg_up

        return {
            "healthy": healthy,
            "ready": bool(getattr(self, "_ready", False)),
            "heartbeat_age": None if age is None else round(age, 1),
            "telegram": tg_up,
            # 断连时长：让 UptimeRobot 这类外部监控也能看到
            # "Telegram 挂了但交易还在跑"这个危险状态。
            # 命令收不到、告警发不出，必须能被外部发现。
            "tg_down_sec": (round(time.time() - self._tg_down_since)
                            if self._tg_down_since else 0),
            "mode": "grid" if self.grid_enabled else "single",
            "sandbox": bool(settings.IS_SANDBOX),
            "blocked": sorted(self.reconciler.blocked) if hasattr(self, "reconciler") else [],
        }

    async def _upload_backup(self, path: str, data: str) -> bool:
        """把备份文件发到 Telegram —— 免费的异地存储"""
        try:
            if not (settings.TG_CHAT_ID and self.tg_app):
                return False
            import os
            await self.tg_app.bot.send_document(
                chat_id=settings.TG_CHAT_ID,
                document=data.encode("utf-8"),
                filename=f"backup_{os.path.basename(path)}",
                caption="💾 自动备份（数据库丢失时可用 /restore 恢复）",
            )
            return True
        except Exception as e:
            logger.warning(f"备份推送 Telegram 失败: {e}")
            return False

    # ---------- 从交易所导入持仓（解锁对账阻塞）----------

    async def _derive_entry_from_trades(self, sym, amount_now):
        """
        从成交历史反推持仓成本。

        为什么需要：本地账本丢失（Render 临时盘重置）时，
        交易所仍有真实持仓，但机器人不知道成本价。
        没有成本价就无法计算止盈止损 —— 这是"暂停"的根源。

        做法：从最近成交往回做 FIFO 配对，
        得到当前持仓量对应的加权平均成本。

        返回 (avg_price, source)；推算不出时回退到当前价并标注来源。
        """
        base = sym.split('/')[0]
        trades = await self.exchange.fetch_my_trades(sym, limit=100)
        if not trades:
            ticker = self.ws.get_ticker(sym)
            px = float(ticker['last']) if ticker else 0.0
            return px, "当前价（无成交历史，成本仅供参考）"

        # 从最近往回，维护一个买入批次栈做 FIFO 配对
        buy_lots = []          # [(qty, price)]
        for t in trades:
            try:
                side = str(t.get('side', '')).lower()
                qty = float(t.get('amount') or 0)
                px = float(t.get('price') or 0)
            except Exception:
                continue
            if qty <= 0 or px <= 0:
                continue
            if side == 'sell':
                # FIFO：与主账本 _consume_position_lots 保持一致。
                # 曾用 LIFO（pop 末尾），测试抓出偏差 ——
                # 复杂买卖交错场景推算 132，正确应为 136。
                need = qty
                while need > 0 and buy_lots:
                    lq, lp = buy_lots[0]
                    take = min(lq, need)
                    lq -= take
                    need -= take
                    if lq <= 1e-12:
                        buy_lots.pop(0)
                    else:
                        buy_lots[0] = (lq, lp)
            elif side == 'buy':
                # 扣除手续费才是真正到手的数量
                fee = t.get('fee') or {}
                fcost = 0.0
                try:
                    fcost = float(fee.get('cost') or 0)
                except Exception:
                    pass
                fcur = str(fee.get('currency') or '').upper()
                net = qty - fcost if fcur == base else qty
                if net > 0:
                    buy_lots.append((net, px))

        remain = amount_now
        cost = 0.0
        got = 0.0
        while remain > 0 and buy_lots:
            lq, lp = buy_lots.pop()
            take = min(lq, remain)
            cost += take * lp
            got += take
            remain -= take
            if got >= amount_now - 1e-12:
                break
        # 必须【覆盖绝大部分持仓】才采用推算结果。
        #
        # 若只匹配到一部分就采用，会造成虚假盈利：
        #   例：实有 5.0 ETH（真实成本 3000），历史只找到 0.1 @100
        #       → 账本成本记 100 → 账面浮盈 +2900%
        #       → 止盈线 1.5% 立刻触发 → 在错误价格卖出
        #       → 账本记"大赚"，实际是平价卖出还要倒贴手续费
        #
        # 覆盖不足时宁可用当前价（浮盈归零，止盈止损从此刻正常起算），
        # 也不要虚假盈利。
        if got > 0 and got >= amount_now * 0.95:
            return cost / got, "成交历史推算"

        ticker = self.ws.get_ticker(sym)
        px = float(ticker['last']) if ticker else 0.0
        if got > 0:
            return px, (f"当前价（历史仅覆盖 {got/amount_now*100:.0f}%，"
                        f"成本仅供参考）")
        return px, "当前价（无可用成交历史，成本仅供参考）"

    async def cmd_import_position(self, update, context):
        """
        /import —— 把交易所真实持仓导入本地账本，解除对账阻塞。

        为什么需要这个命令：
          启动对账发现"本地空、交易所有币"时会暂停该币交易，
          这是正确的保护 —— 防止重复买入。
          但原实现只告诉用户"去核对"，没给解锁路径。
          在 Render 免费层（临时盘）上，数据库会随重启丢失，
          于是每次重启都要人工介入，形成死锁。

          交易所是权威数据源。既然它知道真实持仓，
          就应该允许把它导入账本，而不是永远阻塞。

        用法：
          /import          导入当前被阻塞的币种
          /import ETH/USDT 导入指定币种
          /import all      导入全部有持仓的币种

        注意：导入后盈亏从【导入时刻】起算，
        之前的浮盈浮亏不会体现。
        """
        if not self._auth(update):
            return
        args = list(getattr(context, "args", None) or [])
        # 成本基准模式：
        #   now     以当前价建账（默认）—— 浮盈归零，止盈止损从此刻起算
        #   history 以成交历史推算的成本建账 —— 会带出历史浮盈
        mode = "now"
        words = [a.strip().upper() for a in args]
        if "HISTORY" in words:
            mode = "history"
            words = [w for w in words if w != "HISTORY"]
        target = (words[0] if words else "")

        blocked = set(getattr(self.reconciler, "blocked", set()) or set())
        if target in ("", "ALL"):
            syms = sorted(blocked) if blocked else list(self.symbols)
            if target == "ALL":
                syms = list(self.symbols)
        else:
            syms = [target if "/" in target else target + "/USDT"]
            if syms[0] not in self.symbols:
                await update.effective_message.reply_text(
                    f"❌ {syms[0]} 不在监控列表中")
                return

        if mode == "history":
            await update.effective_message.reply_text(
                "⚠️ 你选择了【历史成本】模式。\n\n"
                "这会把很久以前的买入价写入账本，\n"
                "账面浮盈可能远超止盈线，\n"
                "导致机器人【立即自动卖出】这些持仓。\n\n"
                "如果这不是你的意图，请改用 /import（默认当前价）。")

        if not syms:
            await update.effective_message.reply_text(
                "ℹ️ 当前没有被阻塞的币种，无需导入")
            return

        await update.effective_message.reply_text(
            f"⏳ 正在从交易所导入 {len(syms)} 个币种的持仓…")

        try:
            bal = await self.exchange.fetch_balance()
        except Exception as e:
            await update.effective_message.reply_text(
                f"❌ 读取交易所余额失败: {e}")
            return

        lines = []
        imported = 0
        for sym in syms:
            base = sym.split('/')[0]
            info = bal.get(base) or {}
            free = float(info.get('free') or 0)
            locked = float(info.get('used') or 0)
            total = free + locked
            if total <= 0:
                continue

            ticker = self.ws.get_ticker(sym)
            cur = float(ticker['last']) if ticker else 0.0
            hist_avg, source = await self._derive_entry_from_trades(sym, total)

            # ⚠️ 关键：默认以【当前价】建账，而不是历史成本。
            #
            # 原因（实测）：历史成本会带出巨额账面浮盈 ——
            #   ETH 成本 1930.85，现价 2384.06 → 浮盈 +23.47%
            #   SOL 成本  77.86，现价   99.92 → 浮盈 +28.34%
            # 而止盈线只有 1.5%，_trailing_monitor 每 5 秒一轮，
            # 于是导入后【立刻自动全平】—— 用户完全没预期。
            #
            # 用户 /import 的意图是"解锁阻塞、让机器人接管"，
            # 不是"立刻套现"。所以默认浮盈归零，
            # 止盈止损从【此刻】正常起算。
            #
            # 历史成本只用于展示参考，不参与止盈计算。
            if mode == "history" and hist_avg > 0:
                avg = hist_avg
                basis = f"历史成本（{source}）"
            else:
                avg = cur if cur > 0 else (hist_avg if hist_avg > 0 else 0.0)
                basis = "当前价（盈亏从此刻起算）"
            if avg <= 0:
                lines.append(f"⚠️ {base}  无法取得价格，跳过")
                continue

            # 写入账本
            # 键名必须与买入时一致用 'amount'。
            # v14 误用 'qty'，导致 _bot_position_amount 读不到数量，
            # /panic 静默跳过（不下单、不报错，只回复"全平完成"）。
            # 此处修正，并同时写 qty 作冗余。
            self.position_lots[sym] = [{
                'amount': total, 'qty': total, 'price': avg,
                'cost': total * avg, 'cost_usdt': total * avg,
                'fee': 0.0, 'fee_currency': '', 'fee_usdt': 0.0,
                'time': time.time(),
            }]
            self.entries[sym] = avg
            self.position_counts[sym] = 1
            # 移动止盈的高点也从此刻起算，否则历史高点会立即触发回撤卖出
            self._trailing_high[sym] = cur if cur > 0 else avg
            self.entry_details[sym] = {
                'imported': True, 'basis': basis,
                'hist_cost': hist_avg, 'source': source}
            imported += 1

            # 解除阻塞
            if hasattr(self.reconciler, "unblock"):
                self.reconciler.unblock(sym)

            pnl = ((cur - avg) / avg * 100) if avg > 0 else 0.0
            hist_pnl = (((cur - hist_avg) / hist_avg * 100)
                        if hist_avg > 0 else 0.0)
            lines.append(
                f"✅ {base}  {total:.6f}\n"
                f"     建账基准：{basis}\n"
                f"     现价 {cur:.4f}  记账浮盈 {pnl:+.2f}%\n"
                + (f"     参考：历史成本 {hist_avg:.4f}（约 {hist_pnl:+.2f}%）\n"
                   if hist_avg > 0 and mode != "history" else ""))

        if imported:
            await self._save_runtime_state()
            # 立即推送一份备份，避免下次重启再次丢失
            try:
                await self._push_backup_now()
            except Exception:
                pass
            tip = ("⚠️ 盈亏从【此刻】起算，机器人将按当前价管理这些持仓。"
                   if mode != "history" else
                   "⚠️ 已按【历史成本】建账，账面浮盈可能立即触发止盈卖出。")
            await update.effective_message.reply_text(
                f"📥 已导入 {imported} 个币种\n\n"
                + "\n".join(lines)
                + f"\n\n{tip}"
                "\n已自动推送一份备份到本对话，供以后 /restore 恢复。")
        else:
            await update.effective_message.reply_text(
                "ℹ️ 未发现可导入的持仓（交易所余额均为 0）")

    async def _push_backup_now(self):
        """立即导出并推送一份备份（不等 6 小时）"""
        try:
            path = await self.backup_mgr.export()
            with open(path, "r", encoding="utf-8") as f:
                data = f.read()
            return await self._upload_backup(path, data)
        except Exception as e:
            logger.warning(f"即时备份推送失败: {e}")
            return False

    async def cmd_restore(self, update, context):
        """
        /restore —— 从 JSON 备份恢复数据库。

        用法：把备份文件（.json）直接发给机器人，并回复 /restore；
        或在本地把文件放到 backups/ 目录后执行本命令恢复最近一份。
        """
        if not self._auth(update):
            return
        import glob
        import os
        from core.backup import BackupManager

        # 优先用用户随消息发来的文件
        msg = update.effective_message
        target = None
        if msg and msg.document:
            try:
                f = await msg.document.get_file()
                target = await f.download_as_bytearray()
                target = bytes(target).decode("utf-8")
            except Exception as e:
                return await msg.reply_text(f"读取文件失败: {e}")

        if target is None:
            # 否则取本地最近一份
            files = sorted(glob.glob(
                os.path.join(self.backup_mgr._dir(), "bot_*.json")))
            if not files:
                return await msg.reply_text(
                    "本地没有备份文件。\n"
                    "请把自动备份推送的 .json 文件发给机器人，"
                    "并回复 /restore 进行恢复。")
            with open(files[-1], "r", encoding="utf-8") as fp:
                target = fp.read()

        import storage
        ok = await storage.import_db_from_json(target)
        if not ok:
            return await msg.reply_text("❌ 恢复失败，备份文件可能已损坏")

        # 恢复后立刻重跑对账，防止账本与实盘不符
        await self.reconciler.check_all()
        summary = self.reconciler.summary()
        blocked = self.reconciler.blocked
        text = ("✅ 数据库已从备份恢复\n\n🔍 恢复后对账：\n" + summary)
        if blocked:
            text += ("\n\n⛔ 以下币种已暂停交易，请人工核对后 /resume：\n"
                     + ", ".join(sorted(blocked)))
        else:
            text += "\n\n✅ 对账一致，可直接交易"
        return await msg.reply_text(text, parse_mode="Markdown")

    async def cmd_resetledger(self, update, context):
        """
        /resetledger —— 清空本地持仓账本

        用途：从模拟盘切换到实盘前必须执行。
        模拟盘跑出的持仓只存在于本地账本，切到实盘后真实账户并没有这些币，
        对账会判定"本地有账、交易所为0"而暂停交易。
        本命令把账本清零，让机器人以真实账户为起点重新开始。

        注意：只清内存中的账本并立即落库，不删交易历史。
        执行后请重启机器人，让它重新对账。
        """
        if not self._auth(update):
            return

        before = {sym: len(lots) for sym, lots in self.position_lots.items() if lots}
        cleared = sum(before.values())

        self.position_lots.clear()
        self.position_counts.clear()
        self.entries.clear()
        self.entry_details.clear()
        self._trailing_high.clear()
        # 网格状态一并清除（模拟盘挂出的网格在实盘账户里不存在）
        try:
            self.grid.states.clear()
        except Exception:
            pass
        # 风控计数归零，避免模拟盘的连亏记录延续到实盘
        try:
            self.risk.resume()
        except Exception:
            pass
        # 解除对账造成的暂停
        self.reconciler.clear()

        await self._save_runtime_state()

        if cleared:
            detail = "、".join(f"{k}×{v}" for k, v in before.items())
            text = (f"🧹 已清空本地账本（{cleared} 条记录：{detail}）\n\n"
                    "网格状态与风控计数也已重置。\n"
                    "**请重启机器人**，让它以真实账户余额为起点重新对账。")
        else:
            text = "🧹 本地账本本来就是空的，无需清理。"

        return await update.effective_message.reply_text(text, parse_mode="Markdown")

    def _grid_has_state(self, sym) -> bool:
        """该币种是否处于网格管理之下（有网格状态或挂单）"""
        try:
            st = self.grid.states.get(sym)
            if st is not None and (getattr(st, "lots", None)
                                   or getattr(st, "pending_client_ids", None)):
                return True
        except Exception:
            pass
        return False

    async def _guard_task(self, name, coro_fn):
        """
        包裹后台任务：记录心跳，并在意外退出时告警。

        四个 task 内部都有 try/except 兜底，理论上不会退出。
        但"兜底自身抛异常""初始化阶段出错""asyncio 内部错误"
        都可能让它静默终止，而外部健康检查完全看不出来。
        """
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"❌ 后台任务【{name}】异常退出: {e}", exc_info=True)
            await self._alert(
                f"🚨 后台任务异常退出\n"
                f"   任务: {name}\n"
                f"   错误: {e}\n"
                f"   该任务对应的功能已停止，其余功能不受影响。\n"
                f"   → 建议重启服务", "critical")

    def _check_mode_conflict(self):
        """
        检测交易模式冲突。

        ⚠️ 三个监控任务是独立的 asyncio task，各看各的标志位：

            _auto_trade_monitor  买（单次模式）  条件 auto_trade_enabled
            _grid_monitor        买（网格模式）  条件 grid_enabled
            _trailing_monitor    卖（单次模式）  条件 仅 is_running

        前两者互不看对方的开关，同时开启会并发消费同一份余额。
        第三者更隐蔽：它只判断"有没有持仓"，不判断这个持仓是谁建的。
        于是网格挂单成交后，_trailing_monitor 会用【单次模式的
        tp/sl】去平仓 —— 网格策略被破坏，而网格引擎还以为自己
        持仓完好，下一轮继续挂卖单，状态彻底错乱。

        原实现完全没有这层检查。
        """
        try:
            auto = bool(getattr(self, "auto_trade_enabled", False))
            grid = bool(getattr(self, "grid_enabled", False))
        except Exception:
            return

        if auto and grid:
            self._pending_mode_warning = (
                "⚠️ 检测到模式冲突：自动交易与网格【同时开启】\n"
                "   两个买循环会并发消费同一份余额，且\n"
                "   网格持仓可能被单次模式的止盈止损平掉。\n"
                "   请二选一：\n"
                "     /autotrade off   仅用网格\n"
                "     /gridmode false  仅用单次模式")
            logger.warning("⚠️ 模式冲突：auto_trade 与 grid 同时开启")
            return

        if grid and not auto:
            # 网格模式下提醒止盈参数的归属，避免误调
            logger.info("ℹ️ 网格模式：/settp /setsl 属单次模式参数，"
                        "网格止盈用 /set grid_stop_loss_pct")

    def _start_watchdog_loop(self):
        """
        启动行为巡检（每小时一次）。

        目的：让机器人主动说出"没坏但不对劲"的状态。
        静默地不干活比崩溃危险得多 —— 崩溃你立刻知道，
        装死你能看三天。
        """
        try:
            import asyncio as _aio

            async def _loop():
                await _aio.sleep(300)          # 启动 5 分钟后再开始
                while True:
                    try:
                        if not self.is_running:
                            await _aio.sleep(600)
                            continue
                        alerts = await self.watchdog.check_all()
                        # 趋势退化：看"持续变差"，需查库，失败不影响主流程
                        try:
                            from storage import get_performance_windows
                            w = await get_performance_windows(
                                self.trend.RECENT_N, self.trend.BASELINE_N)
                            alerts += await self.trend.check(w)
                        except Exception as e:
                            logger.debug(f"趋势检测跳过: {e}")
                        for msg in alerts:
                            await self._alert(msg, "warning")
                            await _aio.sleep(2)   # 避免连续发送被限频
                        # 日报（兼心跳证明：收到即说明机器人活着）
                        try:
                            await self.reporter.maybe_send()
                        except Exception as e:
                            logger.debug(f"日报跳过: {e}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug(f"巡检异常: {e}")
                    await _aio.sleep(3600)
            try:
                loop = _aio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(_loop())
            logger.info("🔭 行为巡检已启动（每小时一次）")
        except Exception as e:
            logger.warning(f"行为巡检启动失败（不影响交易）: {e}")

    def _start_backup_loop(self):
        """启动后台定时备份（6 小时一次，滚动保留 7 份）"""
        try:
            import asyncio as _aio
            self._stop_backup = _aio.Event()
            _aio.create_task(self.backup_mgr.loop(self._stop_backup))
            logger.info("💾 自动备份已开启（间隔 6 小时，保留 7 份）")
        except Exception as e:
            logger.warning(f"自动备份启动失败: {e}")

    async def cmd_reconcile(self, update, context):
        """手动重新对账：/reconcile"""
        await self.reconciler.check_all()
        text = "🔍 **持仓对账结果**\n\n" + self.reconciler.summary()
        blocked = self.reconciler.blocked
        if blocked:
            text += "\n\n⛔ 已暂停: " + ", ".join(sorted(blocked))
            text += "\n\n确认仓位无误后可用 `/resume` 解除暂停。"
        else:
            text += "\n\n✅ 无阻塞项"
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _save_runtime_state(self):
        """
        持久化运行时状态（持仓、网格、风控、巡检）。

        ⚠️ 原实现的问题：storage.save_runtime_state 内部 try/except 只
        logger.error，失败时这里毫无感知。后果链：

            保存失败 → 内存状态仍正常，用户完全看不到异常
                     → Render 休眠/重启 → 从库恢复 → 读到【旧状态】
                     → 启动对账发现与交易所不一致 → 🚨 暂停交易

        也就是说：故障在几小时甚至几天后才以"对账失败"的形式暴露，
        而那时已经无法追溯原因。改为失败时立即告警。
        """
        state = {
            'position_counts': self.position_counts,
            'entries': self.entries,
            'trailing_high': self._trailing_high,
            'risk': self.risk.to_dict(),
            'grid_states': {s: g.to_dict() for s, g in self.grid.states.items()},
            'entry_details': self.entry_details,
            'position_lots': self.position_lots,
            'watchdog': self.watchdog.to_dict(),
            'trend': self.trend.to_dict(),
            'retirement': self.retirement.to_dict(),
            'reporter': self.reporter.to_dict(),
            'price_guard': self.price_guard.to_dict(),
        }
        ok = await save_runtime_state(state)
        if not ok:
            # 节流：持久化每轮都调用，失败时不能每轮都告警
            now = time.time()
            if now - self._last_persist_alert > 1800:      # 30 分钟
                self._last_persist_alert = now
                await self._alert(
                    "💾 状态保存失败（已重试）\n"
                    "   持仓/网格状态未能写入数据库。\n"
                    "   机器人在内存中仍正常运行，但一旦重启\n"
                    "   将从【旧状态】恢复，导致对账不一致并暂停交易。\n"
                    "   请检查磁盘空间或数据库连接，并及时 /backup 导出。",
                    "critical")
            return False
        self._persist_fail_count = 0
        return True

    async def _save_config(self):
        """
        持久化全部可调参数。
        此前手工罗列参数名，新增参数时极易漏写导致「改了存不住」。
        改为遍历注册表，新增参数自动纳入持久化。
        """
        from core.params import PARAMS
        cfg = {k: getattr(self, k) for k in PARAMS}
        # 非注册表的状态项
        cfg.update({
            'symbols': self.symbols,
            'timeframe': self.timeframe,
            'auto_trade_enabled': self.auto_trade_enabled,
            'orderbook_filter': self.orderbook_filter,
            'coin_configs': json.dumps(self.coin_configs),
        })
        await save_config(cfg)

    async def _get_cached_tech(self, sym, timeframe='5m', limit=50):
        key = f"{sym}_{timeframe}_{limit}"
        now = time.time()
        if key in self._tech_cache and (now - self._tech_cache_time.get(key, 0)) < self._tech_cache_ttl:
            return self._tech_cache[key]
        tech = await self.tech.calc(sym, timeframe, limit)
        if tech:
            self._tech_cache[key] = tech
            self._tech_cache_time[key] = now
            self._prune_tech_cache(now)
        return tech

    def _prune_tech_cache(self, now=None, max_entries=200, max_age=300.0):
        """
        清理技术缓存。

        原实现只在【切换 K 线周期】时 clear()，平时从不清 ——
        删除币种后其条目会永久驻留。虽然上限很小（约 200KB，
        不构成实际风险），但长期运行的进程不该有无界结构。

        双条件剪枝：超过体积上限，或条目陈旧且超过上限一半。
        """
        now = now if now is not None else time.time()
        cache, times = self._tech_cache, self._tech_cache_time
        if not isinstance(cache, dict):
            return
        # 1) 清掉已失效的键（两个 dict 可能不同步）
        for k in list(times):
            if k not in cache:
                times.pop(k, None)
        if len(cache) <= max_entries:
            return
        # 2) 按年龄排序，先删最旧的
        ordered = sorted(times.items(), key=lambda kv: kv[1])
        target = len(cache) - max_entries // 2
        removed = 0
        for k, ts in ordered:
            if removed >= target:
                break
            if (now - ts) > max_age:
                cache.pop(k, None)
                times.pop(k, None)
                removed += 1
        if removed:
            logger.debug(f"🧹 技术缓存清理 {removed} 条，剩余 {len(cache)}")

    # ---------- 新增：自适应参数更新 ----------
    async def _update_market_adaptive_params(self, sym: str):
        """
        根据技术指标更新【该币种】的自适应参数（每5分钟）。

        原实现用全局单例 + 全局节流，导致一轮循环里只有第一个币种真正计算，
        后面所有币种沿用它的状态 —— 四个波动特性不同的币种会显示同一状态。
        现改为按币种分别存储与节流。
        """
        now = time.time()
        rec = self._adaptive.get(sym)
        if rec and (now - rec.get("ts", 0)) < 300:
            return   # 该币种尚未过期，不重算
        tech = await self._get_cached_tech(sym, self.timeframe, 50)
        if not tech:
            return

        atr = tech.get('atr', 0)
        bb_mid = tech.get('bb_middle', 0)
        bb_width = tech.get('bandwidth_pct', 0) / 100
        trend = tech.get('trend_strength', 0)
        volatility = atr / bb_mid if bb_mid > 0 else 0

        # 状态判定
        if volatility > 0.05:
            state = 'volatile'
        elif volatility < 0.01 and bb_width < 0.02:
            state = 'ultra_low'
        elif abs(trend) > 0.02:
            state = 'trending'
        else:
            state = 'ranging'

        # 参数映射
        if state == 'volatile':
            tp_f, sl_f, off, amt_f = 1.2, 1.5, 8, 0.6
        elif state == 'trending':
            if trend > 0:
                tp_f, sl_f, off, amt_f = 1.5, 1.2, 10, 0.5
            else:
                tp_f, sl_f, off, amt_f = 1.2, 1.4, 8, 0.4
        elif state == 'ranging':
            tp_f, sl_f, off, amt_f = 1.0, 1.0, 0, 1.0
        elif state == 'ultra_low':
            tp_f, sl_f, off, amt_f = 0.8, 0.7, -5, 0.8
        else:
            tp_f, sl_f, off, amt_f = 1.0, 1.0, 0, 1.0

        self._adaptive[sym] = {
            "state": state, "tp_factor": tp_f, "sl_factor": sl_f,
            "offset": off, "amount_factor": amt_f, "ts": now,
        }
        # 镜像（仅用于外部兼容读取，内部逻辑一律用 _adaptive_of(sym)）
        self._adaptive_state = state
        self._adaptive_tp_factor = tp_f
        self._adaptive_sl_factor = sl_f
        self._adaptive_score_offset = off
        self._adaptive_amount_factor = amt_f
        self._adaptive_update_time = now
        logger.info(f"📊 自适应状态 [{sym}]: {state} "
                    f"(波动{volatility:.2%}, 趋势{trend:.2%})")

    def _adaptive_of(self, sym: str) -> dict:
        """
        取【该币种】的自适应参数。未计算过则返回中性默认值（不影响交易）。

        所有内部逻辑都必须走这里，不能直接读 _adaptive_xxx 全局属性，
        否则又会退化成跨币种串用。
        """
        rec = self._adaptive.get(sym)
        if rec:
            return rec
        return {"state": "neutral", "tp_factor": 1.0, "sl_factor": 1.0,
                "offset": 0, "amount_factor": 1.0, "ts": 0.0}

    # ---------- 核心信号（精简5因子 + 自适应调节） ----------
    async def _should_open_position(self, sym, p, tech, funding, fg, usdt_free):
        if tech is None:
            return {'should_open': False, 'score': 50, 'details': ['技术指标缺失']}

        # 更新自适应参数（每5分钟）
        await self._update_market_adaptive_params(sym)

        # 1. RSI评分
        rsi = tech.get('rsi', 50)
        rsi_score = 50 + (50 - rsi) * 0.8
        rsi_score = max(0, min(100, rsi_score))

        # 2. 布林带位置
        bb_lower = tech.get('bb_lower', 0)
        bb_upper = tech.get('bb_upper', 0)
        if bb_upper > bb_lower and p > 0:
            bb_pos = (p - bb_lower) / (bb_upper - bb_lower)
            bb_score = 100 - bb_pos * 100
        else:
            bb_score = 50
        bb_score = max(0, min(100, bb_score))

        # 3. OFI
        ob = self.ws.get_orderbook(sym)
        ofi_score = 50
        if ob:
            bids = ob.get('bids', [])
            asks = ob.get('asks', [])
            if len(bids) >= 5 and len(asks) >= 5:
                bid_vol = sum(b[1] for b in bids[:5])
                ask_vol = sum(a[1] for a in asks[:5])
                ofi = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-6)
                ofi_score = 50 + ofi * 40

        # 4. 趋势过滤器（防止逆势买入）
        trend_strength = tech.get('trend_strength', 0)
        if settings.TREND_FILTER_ENABLED and trend_strength > settings.TREND_THRESHOLD:
            trend_penalty = 30
        elif settings.TREND_FILTER_ENABLED and trend_strength < -settings.TREND_THRESHOLD:
            trend_penalty = 15
        else:
            trend_penalty = 0

        # 5. 成交量因子
        ticker = self.ws.get_ticker(sym)
        vol = ticker.get('volume', 0) if ticker else 0
        vol_score = 50 + min(20, (vol / 1000) * 0.5)

        # 6. 恐惧贪婪因子（此前被硬编码成常数 50，API 请求白跑）
        #    恐惧(低值)=逢低买入机会好 → 高分；贪婪(高值)=追高风险 → 低分
        if fg is None:
            fg_score = 50.0
        else:
            fg_score = max(0.0, min(100.0, 100.0 - float(fg)))

        # 综合评分（权重合计 = 1.0）
        total_score = (rsi_score * 0.25 + bb_score * 0.25 + ofi_score * 0.20 +
                       vol_score * 0.15 + fg_score * 0.15) - trend_penalty
        total_score = max(0, min(100, total_score))

        # 自适应阈值偏移 —— 必须用【本币种】的参数
        ad = self._adaptive_of(sym)
        base_threshold = self._get_coin_param(sym, 'auto_min_score', self.auto_min_score)
        threshold = base_threshold + ad["offset"]
        threshold = max(40, min(95, threshold))

        should_open = total_score >= threshold and trend_penalty < 30

        # 自适应仓位 —— 同样用【本币种】的系数
        base_amount = self._calculate_dynamic_amount(self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt))
        adjusted_amount = base_amount * ad["amount_factor"]

        details = [f"RSI:{rsi:.0f}", f"BB:{bb_score:.0f}", f"OFI:{ofi_score:.0f}",
                   f"FG:{fg_score:.0f}({'NA' if fg is None else int(fg)})",
                   f"趋势:{trend_strength:.2%}", f"惩罚:{trend_penalty}", f"状态:{ad['state']}"]

        # 看门狗：记录评分分布（不管达不达标），用于判断"真没信号"还是"坏了"
        try:
            self.watchdog.record_score(sym, total_score)
        except Exception:
            pass

        return {
            'should_open': should_open,
            'score': total_score,
            'details': details,
            'amount': adjusted_amount,
            'state': ad["state"],
            'adaptive_offset': ad["offset"],
            'adaptive_factor': ad["amount_factor"]
        }
        
        # ---------- 自动交易主循环 ----------
    async def _auto_trade_monitor(self):
        await asyncio.sleep(10)
        while True:
            try:
                if not self.is_running or not self.auto_trade_enabled:
                    await asyncio.sleep(10)
                    continue

                # 看门狗：证明循环还活着（用于区分"没信号"与"卡死"）
                self.watchdog.tick()
                self.watchdog.beat("自动交易")
                self._watchdog_sample_data()

                today = datetime.now(CST).date().isoformat()
                if today != self.last_reset_day:
                    self.daily_trades = 0
                    self.last_reset_day = today
                if self.max_daily_trades > 0 and self.daily_trades >= self.max_daily_trades:
                    await asyncio.sleep(10)
                    continue

                usdt_free = await self._refresh_balance_cache()
                if usdt_free < self.reserve_bottom:
                    await asyncio.sleep(10)
                    continue

                # 退役线：全局累计亏损底线。
                # 与 _check_risk_limits 的区别：那个是局部/周期性的
                # （日内上限、连亏冷却、回撤熔断），会随时间自动恢复；
                # 退役线是【累计、不自动恢复】的最后一道底线。
                try:
                    _net = await get_total_net_profit()
                    _eq = float(self._cached_usdt_free or 0)
                    if not await self.retirement.evaluate(_net, _eq):
                        await asyncio.sleep(60)
                        continue
                except Exception as e:
                    logger.debug(f"退役线检测跳过: {e}")

                if not await self._check_risk_limits():
                    await asyncio.sleep(10)
                    continue

                fg_data = await self.real_data.get_fear_greed_index()
                fg = fg_data["value"] if fg_data else None

                candidates = []
                for sym in self.symbols:
                    try:
                        if self.reconciler.is_blocked(sym):
                            continue
                        if self.position_counts.get(sym, 0) >= self.max_positions_per_coin:
                            continue
                        if self._bot_position_cost(sym) >= self.max_per_coin_usdt:
                            continue

                        ticker = self.ws.get_ticker(sym)
                        if not ticker:
                            continue
                        p = ticker['last']

                        # 价格突变保护：拦截 API 抽风 / 瞬时插针推送的错误价格。
                        # 原代码只有"行情陈旧"检测（时间戳），
                        # 识别不了【数据是新鲜的错误值】这种情况。
                        if not await self.price_guard.check(sym, p):
                            continue

                        tech = await self._get_cached_tech(sym, self.timeframe, 50)
                        if not tech:
                            continue

                        decision = await self._should_open_position(sym, p, tech, None, fg, usdt_free)
                        if not decision['should_open']:
                            continue

                        if self.orderbook_filter:
                            ob = self.ws.get_orderbook(sym)
                            if ob:
                                bids = ob.get('bids', [])
                                asks = ob.get('asks', [])
                                if not bids or not asks:
                                    continue
                                spread = (asks[0][0] - bids[0][0]) / bids[0][0]
                                if spread > 0.002:
                                    continue

                        amount_usdt = decision.get('amount', self.single_order_usdt)
                        remaining = self.max_per_coin_usdt - self._bot_position_cost(sym)
                        if remaining <= 0:
                            continue
                        amount_usdt = min(amount_usdt, remaining)

                        if not await self._can_allocate(amount_usdt):
                            continue

                        candidates.append((decision['score'], sym, p, amount_usdt))
                    except Exception as e:
                        logger.error(f"候选生成异常 {sym}: {e}")

                candidates.sort(key=lambda x: x[0], reverse=True)
                opened = set()
                for score, sym, p, amount_usdt in candidates:
                    if sym in opened:
                        continue
                    if usdt_free < amount_usdt + self.reserve_bottom:
                        continue

                    raw_amount = amount_usdt / p
                    rounded = await self._round_amount_by_precision(sym, raw_amount)
                    if rounded <= 0:
                        continue

                    # 与卖出操作串行，避免下单 I/O 期间账本被并发修改
                    async with await self._sym_lock(sym):
                        order = await self.exchange.create_market_buy_order(sym, rounded)
                        if not order:
                            continue
                        self.daily_trades += 1
                        self.watchdog.record_open(sym)
                        filled = float(order.get('filled') or 0)
                        avg = float(order.get('average') or p)
                        # 滑点检测：市价单无法事前限价，
                        # 只能事后比对"下单时看到的价格"与"实际成交均价"。
                        # 偏离过大说明盘口深度不足或行情剧变。
                        try:
                            await self.price_guard.check_slippage(
                                sym, p, avg, side="buy")
                        except Exception as e:
                            logger.debug(f"滑点检测跳过 {sym}: {e}")
                        fee = float(order.get('_fee_cost') or 0)
                        fee_currency = order.get('_fee_currency', '')
                        base = sym.split('/')[0]
                        net_amount = filled - fee if fee_currency == base else filled
                        real_cost = filled * avg + (fee if fee_currency in ('', 'USDT') else 0)
                        # ⚠️ 手续费折算：以【币的数量】计的手续费必须先换成 USDT。
                        # 原实现直接把 lot['fee']（币数）当 USDT 从盈亏里减，
                        # 而这部分已通过 net_amount 减少体现过一次 —— 重复扣。
                        # 误差与币价成反比：ETH 可忽略，DOGE 每100U少算0.53U，
                        # SHIB 这类极低价币会算出荒谬结果。
                        if fee_currency == base:
                            # base 币扣费：已体现在 net_amount 里，成本不再重复计
                            fee_usdt = 0.0
                        else:
                            # quote 币（USDT）或未知币种：按面值计入
                            fee_usdt = float(fee)
                        if net_amount <= 0 or real_cost <= 0:
                            # 原实现直接 _is_paused=True，单次下单抖动即导致机器人永久停摆
                            await self._alert(f"{sym} 买单异常(filled={filled})，跳过本轮", "critical")
                            continue

                        self._append_position_lot(sym, net_amount, avg, filled * avg,
                                                  fee, fee_currency, fee_usdt=fee_usdt)
                        self.entry_details[sym] = {'signal_score': score, 'fear_greed': fg}
                        disp_t, iso_t = now_parts()
                        await save_trade_detail({
                            'time': disp_t, 'ts': iso_t, 'symbol': sym, 'side': 'buy',
                            'price': avg, 'amount': filled, 'signal_score': score, 'fear_greed': fg or 0,
                            'funding_rate': 0, 'pnl_pct': 0, 'real_cost': real_cost,
                            'fee': fee, 'fee_currency': fee_currency, 'order_id': order.get('id','')
                        })
                        await self._refresh_balance_cache(force=True)
                        usdt_free = self._cached_usdt_free
                        await self._save_runtime_state()
                        # 持仓变化后立刻异地备份。
                        # 原实现等 6 小时，而 Render 随时可能重启，
                        # 于是多数情况下备份还没攒下第一份就丢了。
                        asyncio.create_task(self._push_backup_now())
                        opened.add(sym)
                        if settings.TG_CHAT_ID and self.tg_app:
                            try:
                                await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,
                                    text=f"🤖 开仓 {sym} {amount_usdt:.2f}U @ {avg:.4f} 仓位{self.position_counts[sym]}/{self.max_positions_per_coin} | 评分{score:.0f}")
                            except Exception as e:
                                logger.warning(f"开仓通知发送失败: {e}")
                    await asyncio.sleep(1)
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"自动交易循环错误: {e}")
                await asyncio.sleep(30)

    # ---------- 移动止盈止损 ----------
    async def _trailing_monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                if not self.is_running:
                    await asyncio.sleep(5)
                    continue

                await self._refresh_balance_cache()
                self.watchdog.beat("移动止盈止损")
                for sym in self.symbols:
                    try:
                        if self.position_counts.get(sym, 0) <= 0:
                            continue
                        # ⚠️ 网格模式下不得插手：本任务用的是【单次模式】的
                        # tp/sl/移动止损，而网格持仓应由网格引擎自己的
                        # 间距与区间止损管理。若在此用单次参数平仓，
                        # 会在网格预期之外卖出，且网格仍以为持仓完好，
                        # 下一轮继续挂卖单 —— 状态错乱。
                        if getattr(self, "grid_enabled", False):
                            if self._grid_has_state(sym):
                                continue
                        ticker = self.ws.get_ticker(sym)
                        if not ticker:
                            continue
                        p = ticker['last']
                        amount = self._bot_position_amount(sym)
                        if amount <= 0:
                            self.position_counts[sym] = 0
                            await self._save_runtime_state()
                            continue

                        entry = self._weighted_entry(sym) or self.entries.get(sym, p)
                        detail = self.entry_details.get(sym, {})
                        # 自适应止盈止损 —— 用【本币种】的因子
                        adx = self._adaptive_of(sym)
                        base_tp = self._get_coin_param(sym, 'tp_pct', self.tp_pct)
                        base_sl = self._get_coin_param(sym, 'sl_pct', self.sl_pct)
                        tp = base_tp * adx["tp_factor"]
                        sl = base_sl * adx["sl_factor"]
                        # 限制范围
                        # 安全边界：原为硬编码 0.06 / 0.002 / 0.04 / 1.2，
                        # 用户设了更大的止盈会被静默吞掉且无任何提示。
                        # 现改为可调参数，并在真正夹取时告警。
                        tp_cap = self._get_coin_param(sym, 'tp_max_pct', self.tp_max_pct)
                        sl_lo = self._get_coin_param(sym, 'sl_min_pct', self.sl_min_pct)
                        sl_hi = self._get_coin_param(sym, 'sl_max_pct', self.sl_max_pct)
                        min_ratio = self._get_coin_param(sym, 'tp_sl_min_ratio', self.tp_sl_min_ratio)

                        clamped = []
                        if tp > tp_cap:
                            clamped.append(f"止盈 {tp*100:.2f}%→{tp_cap*100:.2f}%(上限)")
                            tp = tp_cap
                        if tp < self.breakeven_pct:
                            clamped.append(f"止盈 {tp*100:.2f}%→{self.breakeven_pct*100:.2f}%(保本线)")
                            tp = self.breakeven_pct
                        if sl > sl_hi:
                            clamped.append(f"止损 {sl*100:.2f}%→{sl_hi*100:.2f}%(上限)")
                            sl = sl_hi
                        if sl < sl_lo:
                            clamped.append(f"止损 {sl*100:.2f}%→{sl_lo*100:.2f}%(下限)")
                            sl = sl_lo
                        if sl > 0 and tp / sl < min_ratio:
                            new_tp = sl * min_ratio
                            clamped.append(f"止盈 {tp*100:.2f}%→{new_tp*100:.2f}%(维持{sl_lo and min_ratio or min_ratio}倍比值)")
                            tp = new_tp
                        if clamped:
                            logger.warning(
                                f"⚠️ {sym} 止盈止损被安全边界夹取: {', '.join(clamped)}\n"
                                f"   如需放宽: /set tpmax_pct <值> 或 /set slmax_pct <值>")

                        high = self._trailing_high.get(sym, entry)
                        if high <= 0:
                            high = entry          # 部分成交后 high 被误置 0 会导致移动止盈永远失效

                        profit_pct = (p - entry) / entry * 100
                        # 最高点盈利：移动止损的激活线要按【曾经到达过的高度】判定，
                        # 而不是当前盈利 —— 否则回撤过程中当前盈利已低于激活线，
                        # 移动止损永远启动不了（等于废掉这个功能）。
                        peak_pct = (high - entry) / entry * 100 if high > 0 else 0.0
                        tsl = self._get_coin_param(sym, 'trailing_sl_pct', self.trailing_sl_pct)
                        ttp = self._get_coin_param(sym, 'trailing_tp_pct', self.trailing_tp_pct)
                        tsl_arm = self._get_coin_param(sym, 'trailing_sl_arm_pct',
                                                       self.trailing_sl_arm_pct)

                        # 移动止盈/止损的卖出价不得低于【保本线】。
                        # 否则"回撤卖出"会在扣完手续费后反而亏钱 ——
                        # 移动止盈本意是锁定利润，不该锁出亏损。
                        # 保本线 = 开仓价 × (1 + 往返手续费 + 最低利润要求)
                        floor_price = entry * (1 + self.breakeven_pct)

                        # 判定顺序：先硬止损 → 完整止盈 → 移动止盈 → 移动止损
                        # （原实现把 'profit >= tp*0.5' 放在最前，导致 take_profit 分支永不执行）
                        if profit_pct <= -sl * 100:
                            reason, action = "stop_loss", 'sell'
                        elif profit_pct >= tp * 100:
                            reason, action = "take_profit", 'sell'
                        elif profit_pct >= tp * 100 * 0.5 and p <= max(high * (1 - ttp), floor_price):
                            # 高盈利区回撤：锁定利润（但不锁出亏损）
                            reason, action = "trailing_tp", 'sell'
                        elif (tsl > 0 and peak_pct >= tsl_arm * 100
                              and p <= max(high * (1 - tsl), floor_price)):
                            # 移动止损：必须先盈利到激活线才启动，
                            # 且卖出价不低于保本线。
                            # 原实现 `profit_pct > 0` 会让币价随便波动 0.7%
                            # 就被洗出去，扣完手续费是净亏的。
                            reason, action = "trailing_sl", 'sell'
                        else:
                            reason, action = "", 'hold'

                        if p > high:
                            self._trailing_high[sym] = p

                        if action == 'sell':
                            # 与买入操作串行：下单 I/O 期间账本不得被并发修改
                            async with await self._sym_lock(sym):
                                amount = self._bot_position_amount(sym)
                                if amount <= 0:
                                    continue
                                rounded = await self._round_amount_by_precision(sym, amount)
                                if rounded <= 0:
                                    continue
                                sell_order = await self.exchange.create_market_sell_order(sym, rounded)
                                if not sell_order:
                                    continue
                                sell_filled = float(sell_order.get('filled') or 0)
                                sell_avg = float(sell_order.get('average') or p)
                                # 滑点检测（卖出方向）：
                                # 卖便宜了同样是真金白银的损失
                                try:
                                    await self.price_guard.check_slippage(
                                        sym, p, sell_avg, side="sell")
                                except Exception as e:
                                    logger.debug(f"滑点检测跳过 {sym}: {e}")
                                sell_revenue = sell_filled * sell_avg
                                sell_fee = float(sell_order.get('_fee_cost') or 0)
                                try:
                                    net_pnl, real_cost, _ = self._consume_position_lots(sym, sell_filled, sell_avg, sell_revenue, sell_fee)
                                except ValueError as e:
                                    # 原实现直接置 _is_paused=True 且无恢复手段，一次账本抖动即永久停摆
                                    logger.error(f"账本不一致 {sym}: {e}，跳过本轮，不暂停整个机器人")
                                    continue
                            net_pnl_pct = (net_pnl / real_cost * 100) if real_cost > 0 else 0
                            if net_pnl < 0:
                                self._consecutive_losses += 1
                                self._today_loss_pct += abs(net_pnl_pct) / 100
                            else:
                                self._consecutive_losses = 0

                            disp_t, iso_t = now_parts()
                            trade = {"time": disp_t, "ts": iso_t, "symbol": sym,
                                     "entry": entry, "exit": p, "pnl_pct": ((p-entry)/entry*100),
                                     "net_pnl": net_pnl, "net_pnl_pct": net_pnl_pct}
                            await save_trade(trade)
                            self.trades.insert(0, trade)
                            await save_trade_detail({"time": disp_t, "ts": iso_t, "symbol": sym, "side": "sell",
                                                     "price": sell_avg, "amount": sell_filled, "pnl_pct": ((p-entry)/entry*100),
                                                     "signal_score": detail.get('signal_score',0), "fear_greed": detail.get('fear_greed',0),
                                                     "real_revenue": sell_revenue, "fee": sell_fee, "order_id": sell_order.get('id',''),
                                                     "net_pnl_pct": net_pnl_pct})
                            # 原实现无条件置 0，部分成交后移动止盈/止损会永久失效
                            if self.position_counts.get(sym, 0) > 0:
                                self._trailing_high[sym] = max(
                                    self._trailing_high.get(sym, 0), p
                                )
                            else:
                                self._trailing_high.pop(sym, None)
                            await self._save_runtime_state()
                            asyncio.create_task(self._push_backup_now())
                            if settings.TG_CHAT_ID and self.tg_app:
                                try:
                                    await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,
                                        text=f"📉 {reason} {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
                                except Exception as e:
                                    logger.warning(f"平仓通知发送失败: {e}")
                    except Exception as e:
                        logger.error(f"追踪异常 {sym}: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"追踪任务异常: {e}")
                await asyncio.sleep(5)

    # ---------- 网格主循环 ----------
    async def _grid_monitor(self):
        """
        网格模式主循环。

        与单次模式的关键差异：
          - 下单交给 executor.sync_symbol()，它把「目标订单」同步到交易所，
            重启后自动与交易所未成交单对账，无需手工恢复
          - 每档独立配对，盈利来自每一格价差
          - 趋势过滤 + 区间止损，防止在单边下跌中一路建仓
        """
        await asyncio.sleep(8)
        while True:
            try:
                if not self.is_running or not self.grid_enabled:
                    await asyncio.sleep(10)
                    continue
                self.watchdog.beat("网格交易")
                if not self.auto_trade_enabled:
                    await asyncio.sleep(10)
                    continue

                await self._refresh_balance_cache()
                equity = self._effective_equity_usdt()
                if equity <= 0:
                    await asyncio.sleep(15)
                    continue

                # 风险监控更新峰值与回撤
                self.risk.update_equity(equity)

                for sym in self.symbols:
                    try:
                        if self.reconciler.is_blocked(sym):
                            continue
                        await self._grid_step(sym, equity)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"网格循环异常 {sym}: {e}")
                    await asyncio.sleep(1)

                await self._save_runtime_state()
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"网格主循环异常: {e}")
                await asyncio.sleep(20)

    async def _grid_step(self, sym, equity):
        """单个币种的网格推进"""
        ticker = self.ws.get_ticker(sym)
        if not ticker:
            return
        # 数据陈旧检测：断网时不基于过期价格下单
        ts = float(ticker.get('timestamp') or 0)
        if ts > 0 and (time.time() - ts) > float(self.data_max_age):
            logger.debug(f"{sym} 行情陈旧，跳过本轮")
            return

        p = float(ticker.get('last') or 0)
        if p <= 0:
            return

        # 1) 区间止损优先
        if self.grid.should_stop_loss(sym, p):
            await self._alert(f"🕸️ {sym} 击穿网格区间下限，触发止损清仓", "critical")
            if await self.executor.liquidate(sym, p):
                self.grid.reset(sym, p)
                self.risk.record_close(-1, abs(float(self.grid_stop_loss_pct)) * 100)
                await self._save_runtime_state()
            return

        # 2) 计算 ATR（决定动态间距）
        tech = await self._get_cached_tech(sym, self.timeframe, 50)
        atr_pct = SignalEngine.atr_pct(tech) if tech else 0.0
        self._last_atr_pct = atr_pct

        # 3) 趋势过滤：下跌趋势中不新开网格（已有持仓继续吃网格利润）
        allowed, why = SignalEngine.grid_entry_allowed(tech, self)
        st = self.grid.get_state(sym)
        if not allowed and st is None:
            logger.debug(f"{sym} 暂不开网: {why}")
            return

        # 4) 风控闸门
        used = await self._allocation_used_usdt()
        ok, reason = await self.risk.can_open(
            sym, extra_usdt=0.0, used_usdt=used, equity=equity)
        if not ok and st is None:
            logger.debug(f"{sym} 风控拦截: {reason}")
            return

        # 5) 同步订单到交易所（含对账与成交检测）
        res = await self.executor.sync_symbol(sym, p, atr_pct, equity)
        if res.get("filled"):
            await self._save_runtime_state()
        if res.get("placed") or res.get("cancelled"):
            logger.info(f"🕸️ {sym} 网格同步: 挂{res['placed']} 撤{res['cancelled']}")

    # ---------- 风险监控 ----------
    async def _risk_monitor_task(self):
        await asyncio.sleep(5)
        while self.is_running:
            try:
                await self._refresh_balance_cache(force=True)
                self.watchdog.beat("风控监控")
                equity = self._cached_usdt_free
                for sym in self.symbols:
                    ticker = self.ws.get_ticker(sym)
                    if ticker:
                        coin = sym.split('/')[0]
                        free = self._cached_balances.get(coin, {}).get('free', 0)
                        equity += free * ticker.get('last', 0)
                if equity > 0:
                    if self._daily_start_equity <= 0:
                        self._daily_start_equity = equity
                    self._today_loss_usdt = max(0.0, self._daily_start_equity - equity)
                    self._today_loss_pct = self._today_loss_usdt / self._daily_start_equity if self._daily_start_equity > 0 else 0.0
                    self.peak_total_value = max(self.peak_total_value or equity, equity)
                    drawdown = (self.peak_total_value - equity) / self.peak_total_value if self.peak_total_value > 0 else 0.0
                    self._drawdown_safe_flag = drawdown < self.max_drawdown_pct
                    self._last_drawdown = drawdown
                    # 告警统一交给 _check_risk_limits，避免此处每 30s 重复刷屏
                await self._save_runtime_state()
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"风险监控异常: {e}")
                await asyncio.sleep(30)

    # ---------- Panic Sell ----------
    async def panic_sell_all(self):
        """
        紧急全平。返回每个币种的结果明细。

        ⚠️ 原实现无条件回复「🚨 全平完成」，
        无论是否真的卖出过。实测问题链：
          /import 写入键名 'qty' → _bot_position_amount 读 'amount'
          → amount = 0 → continue（静默跳过）
          → 用户看到"全平完成"，交易所持仓纹丝不动

        改为逐币种记录结果，由调用方如实汇报。
        """
        self._is_paused = True
        results = []
        for sym in self.symbols:
            try:
                await self.exchange.cancel_all_orders(sym)
                amount = self._bot_position_amount(sym)
                if amount <= 0:
                    results.append((sym, "skip", 0.0, "账本无持仓"))
                    continue
                rounded = await self._round_amount_by_precision(sym, amount)
                if rounded <= 0:
                    results.append((sym, "fail", amount,
                                    "数量低于交易所最小精度，已跳过"))
                    continue
                order = await self.exchange.create_market_sell_order(sym, rounded)
                if not order:
                    results.append((sym, "fail", rounded, "下单未返回结果"))
                    continue
                filled = float(order.get('filled') or 0)
                avg = float(order.get('average') or 0)
                revenue = filled * avg
                fee = float(order.get('_fee_cost') or 0)
                if filled <= 0:
                    results.append((sym, "fail", rounded, "订单未成交"))
                    continue
                try:
                    self._consume_position_lots(sym, filled, avg, revenue, fee)
                except ValueError:
                    logger.error(f"Panic Sell 账本不一致 {sym}")
                if self._bot_position_amount(sym) <= 1e-12:
                    self.position_counts[sym] = 0
                results.append((sym, "sold", filled, f"均价 {avg:.4f}"))
            except Exception as e:
                logger.error(f"Panic Sell {sym} 失败: {e}")
                results.append((sym, "error", 0.0, str(e)[:60]))
        await self._save_runtime_state()
        return results

    # ---------- Telegram 命令（精简） ----------
    async def cmd_menu(self, update, context):
        if not self._auth(update): return
        await update.effective_message.reply_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())

    def _build_main_keyboard(self):
        auto = "🟢" if self.auto_trade_enabled else "🔴"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平", callback_data="panic_confirm")],
            [InlineKeyboardButton("⚡ 开启", callback_data="bot_start"), InlineKeyboardButton("🔴 关机", callback_data="bot_stop")],
            [InlineKeyboardButton(f"🤖 自动交易 {auto}", callback_data="toggle_auto")],
            [InlineKeyboardButton("📊 状态", callback_data="status"), InlineKeyboardButton("💳 余额", callback_data="balance")],
            [InlineKeyboardButton("📋 持币", callback_data="holdings"), InlineKeyboardButton("📊 信号", callback_data="check")],
            [InlineKeyboardButton("🎯 止盈", callback_data="menu_set_tp"), InlineKeyboardButton("🛡️ 止损", callback_data="menu_set_sl")],
            [InlineKeyboardButton("📉 移损", callback_data="menu_set_tsl"), InlineKeyboardButton("🏹 移盈", callback_data="menu_set_tmpt")],
            [InlineKeyboardButton("💵 额度", callback_data="menu_set_amount"), InlineKeyboardButton("⏱ 周期", callback_data="menu_set_tf")],
            [InlineKeyboardButton("🔒 底线", callback_data="menu_set_reserve"), InlineKeyboardButton("🔢 上限", callback_data="menu_set_trades")],
            [InlineKeyboardButton("➕ 币种", callback_data="menu_add_symbol"), InlineKeyboardButton("➖ 币种", callback_data="menu_del_symbol")],
            [InlineKeyboardButton("🕸️ 网格", callback_data="grid"), InlineKeyboardButton("⚙️ 参数", callback_data="params")],
            [InlineKeyboardButton("📈 仪表盘", callback_data="stats"), InlineKeyboardButton("💾 备份", callback_data="backup")],
            [InlineKeyboardButton("✅ 解除熔断", callback_data="resume"), InlineKeyboardButton("🔄 刷新", callback_data="refresh")],
            [InlineKeyboardButton("📥 导入交易所持仓", callback_data="import_pos")],
        ])

    async def cmd_status(self, update, context):
        if not self._auth(update): return
        bal = await self.exchange.fetch_balance()
        usdt = float(bal.get('USDT', {}).get('free', 0))
        total = usdt
        pos_lines = []
        for sym in self.symbols:
            ticker = self.ws.get_ticker(sym)
            if ticker:
                p = ticker['last']
                coin = sym.split('/')[0]
                free = float(bal.get(coin, {}).get('free', 0))
                val = free * p
                total += val
                count = self.position_counts.get(sym, 0)
                entry = self.entries.get(sym, 0)
                pnl = f" | {'🟢' if p>=entry else '🔴'} {((p-entry)/entry*100):+.2f}%" if entry else ""
                pos_lines.append(f"• {sym}: {free:.4f} 价值{val:.2f}U 仓位{count}/{self.max_positions_per_coin}{pnl}")
        lines = [
            f"📊 状态 {self.env_tag}",
            f"💰 总资产: {total:.2f}U | 可用: {usdt:.2f}U",
            "📈 持仓:",
            *pos_lines,
            "━━━━━━━━━━",
            f"止盈 {self.tp_pct:.1%} 止损 {self.sl_pct:.1%} 移损 {self.trailing_sl_pct:.1%} 移盈 {self.trailing_tp_pct:.1%}",
            f"今日交易 {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades>0 else '∞'}",
            f"日亏损 {self._today_loss_pct*100:.1f}% / {self.max_daily_loss_pct*100:.0f}%",
            f"回撤 {self.max_drawdown_pct*100:.0f}% | 暂停 {'是' if self._is_paused else '否'}",
        ]
        # 自适应状态按币种分别显示 —— 原来只显示一个全局变量，
        # 会让人误以为所有币种都处于同一状态（实际是串用的结果）。
        ad_lines = []
        for sym in self.symbols:
            a = self._adaptive.get(sym)
            if a:
                ad_lines.append(f"{sym.split('/')[0]}:{a['state']}"
                                f"({a['offset']:+d}分,{a['amount_factor']:.2f}x)")
        lines.append("自适应: " + (" ".join(ad_lines) if ad_lines else "尚未计算"))
        try:
            lines.append(f"巡检: {self.watchdog.summary()}")
        except Exception:
            pass
        try:
            cap = float(getattr(self, 'equity_cap_usdt', 0) or 0)
            if cap > 0:
                real = self._total_equity_usdt()
                eff = self._effective_equity_usdt()
                if abs(real - eff) > 0.01:
                    lines.append(f"权益: 实际 {real:.2f}U → 按 {eff:.2f}U 计算仓位"
                                 f"（上限 {cap:.0f}U）")
        except Exception:
            pass
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_check(self, update, context):
        if not self._auth(update): return
        lines = ["📈 信号检查\n"]
        shown = 0
        for sym in self.symbols:
            ticker = self.ws.get_ticker(sym)
            if not ticker:
                continue
            p = ticker['last']
            tech = await self._get_cached_tech(sym, self.timeframe, 50)
            if not tech:
                continue
            decision = await self._should_open_position(sym, p, tech, None, None, self._cached_usdt_free)
            status = "🎯 可开仓" if decision['should_open'] else "⏳ 等待"
            lines.append(f"{sym}: {p:.2f} | 评分{decision['score']:.0f} | {status}")
            lines.append(f"   {', '.join(decision['details'])}")
            lines.append(f"   偏移{decision.get('adaptive_offset',0):+d}分, 仓位{decision.get('adaptive_factor',1.0):.2f}x")
            shown += 1

        # 原实现：无数据时只输出"📈 信号检查"四个字，
        # 用户分不清是【没配置币种】还是【行情没到】，无从下手。
        if shown == 0:
            if not self.symbols:
                lines.append("⚠️ 监控列表为空。")
                lines.append("   用 /addsymbol ETH/USDT 添加币种")
            else:
                lines.append("⚠️ 暂无行情数据。可能原因：")
                lines.append("   · WebSocket 尚未连接（刚启动，稍等片刻）")
                lines.append("   · K 线数据未就绪")
                lines.append("   持续无数据请发 /patrol 或查看日志")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_holdings(self, update, context):
        if not self._auth(update): return
        bal = await self.exchange.fetch_balance()
        lines = ["📋 持币"]
        for sym in self.symbols:
            coin = sym.split('/')[0]
            free = float(bal.get(coin, {}).get('free', 0))
            if free > 0.0001:
                ticker = self.ws.get_ticker(sym)
                p = ticker['last'] if ticker else 0
                val = free * p
                count = self.position_counts.get(sym, 0)
                entry = self.entries.get(sym, 0)
                pnl = f" | {'🟢' if p>=entry else '🔴'} {((p-entry)/entry*100):+.2f}%" if entry else ""
                lines.append(f"• {sym}: {free:.4f} 价值{val:.2f}U 仓位{count}/{self.max_positions_per_coin}{pnl}")
        await update.effective_message.reply_text("\n".join(lines) if len(lines)>1 else "暂无持仓")

    async def cmd_panic(self, update, context):
        if not self._auth(update): return
        results = await self.panic_sell_all()

        sold, failed, skipped = [], [], []
        for sym, status, qty, note in results:
            base = sym.split('/')[0]
            if status == "sold":
                sold.append(f"✅ {base}  卖出 {qty:.6f}  {note}")
            elif status in ("fail", "error"):
                failed.append(f"❌ {base}  {note}")
            else:
                skipped.append(f"· {base}  {note}")

        sold_n = len(sold)
        if sold_n:
            head = f"🚨 全平完成 —— 已卖出 {sold_n} 个币种"
        elif failed:
            head = "🚨 全平失败 —— 没有任何持仓被卖出"
        else:
            head = "🚨 全平完成 —— 但账本中本就无持仓"

        lines = [head, ""]
        lines += sold
        if failed:
            lines.append("")
            lines += failed
        if skipped:
            lines.append("")
            lines.append("未处理：")
            lines += skipped

        # 存在失败时明确提示，避免"显示完成实际没卖"
        if failed:
            lines.append("")
            lines.append("⚠️ 交易所可能仍有持仓，请到交易所确认，")
            lines.append("   必要时手动卖出或用 /reconcile 核对")

        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_autotrade(self, update, context):
        if not self._auth(update): return
        try:
            mode = context.args[0].lower()
            if mode == "on":
                self.auto_trade_enabled = True
            elif mode == "off":
                self.auto_trade_enabled = False
            else:
                raise ValueError
            await self._save_config()
            await update.effective_message.reply_text(f"🤖 自动交易已{'开启' if self.auto_trade_enabled else '关闭'}")
        except:
            await update.effective_message.reply_text("用法: /autotrade on|off")

    # ---------- 通用参数命令（由 params 注册表驱动）----------
    # 此前每个参数各写一个命令函数，导致 11 个函数结构雷同、校验规则不一致，
    # 且部分用裸 except: pass 静默吞掉输入错误。
    # 现在统一由注册表驱动，新增参数无需改动命令层。

    async def cmd_set(self, update, context):
        """/set <参数名> <值> —— 修改任意可调参数"""
        if not self._auth(update):
            return
        from core.params import PARAMS, parse, display, range_hint
        args = context.args or []
        if len(args) < 2:
            return await update.effective_message.reply_text(
                "❌ 用法: /set <参数名> <值>\n"
                "例: /set grid_levels 10\n"
                "查看全部可调参数: /params")

        key = args[0].strip().lower()
        # 支持别名（如 /set tp 等价于 tp_pct）
        if key not in PARAMS:
            cands = [k for k in PARAMS if k.startswith(key)]
            if len(cands) == 1:
                key = cands[0]
            elif len(cands) > 1:
                return await update.effective_message.reply_text(
                    f"❌ 参数名不明确，匹配到: {', '.join(cands)}")
            else:
                return await update.effective_message.reply_text(
                    f"❌ 未知参数: {args[0]}\n用 /params 查看全部可调参数")

        val, err = parse(key, args[1])
        if err:
            spec = PARAMS[key]
            return await update.effective_message.reply_text(
                f"❌ {err}\n范围: {range_hint(spec)}")

        old = getattr(self, key, None)
        setattr(self, key, val)
        await self._save_config()
        spec = PARAMS[key]
        return await update.effective_message.reply_text(
            f"✅ {spec.desc}\n"
            f"   {display(key, old)}  →  {display(key, val)}")

    async def cmd_get(self, update, context):
        """/get <参数名> —— 查看当前值"""
        if not self._auth(update):
            return
        from core.params import PARAMS, display, range_hint
        args = context.args or []
        if not args:
            return await update.effective_message.reply_text("❌ 用法: /get <参数名>")
        key = args[0].strip().lower()
        if key not in PARAMS:
            cands = [k for k in PARAMS if k.startswith(key)]
            if len(cands) == 1:
                key = cands[0]
            else:
                return await update.effective_message.reply_text(f"❌ 未知参数: {args[0]}")
        spec = PARAMS[key]
        return await update.effective_message.reply_text(
            f"⚙️ {spec.desc}\n"
            f"   当前: {display(key, getattr(self, key, spec.default))}\n"
            f"   默认: {display(key, spec.default)}\n"
            f"   范围: {range_hint(spec)}\n"
            f"   命令: /set {key} <值>")

    async def cmd_params(self, update, context):
        """/params —— 列出全部可调参数"""
        if not self._auth(update):
            return
        from core.params import GROUPS, display
        args = context.args or []
        lines = ["⚙️ 可调参数（/set <名> <值> 修改）", ""]
        for grp in ["网格·档位", "网格·中枢", "网格·资金", "网格·风控",
                    "执行", "通用风控", "单次模式", "自适应"]:
            specs = GROUPS.get(grp)
            if not specs:
                continue
            if args and args[0].lower() not in grp.lower():
                continue
            lines.append(f"【{grp}】")
            for s in specs:
                cur = display(s.key, getattr(self, s.key, s.default))
                lines.append(f"  {s.key} = {cur}")
            lines.append("")
        if len(lines) > 2:
            lines.append("💡 /params 网格  只看网格类；/get <名> 看详情")
        return await update.effective_message.reply_text("\n".join(lines))

    async def cmd_alias(self, update, context):
        """兼容旧命令：/settp /setsl /setlevels ... 全部转发到 /set"""
        if not self._auth(update):
            return
        from core.params import ALIAS_MAP
        cmd = (update.effective_message.text or "").split()[0].lstrip("/").split("@")[0]
        key = ALIAS_MAP.get(cmd.lower())
        if not key:
            return await update.effective_message.reply_text(f"❌ 未知命令 {cmd}")
        args = context.args or []
        if not args:
            from core.params import PARAMS, range_hint
            spec = PARAMS[key]
            return await update.effective_message.reply_text(
                f"⚙️ {spec.desc}\n用法: /{cmd} <值>\n范围: {range_hint(spec)}")
        # 复用 /set 的处理逻辑
        context.args = [key, args[0]]
        return await self.cmd_set(update, context)

    async def cmd_gridstatus(self, update, context):
        """/grid —— 查看各币种网格运行状态"""
        if not self._auth(update):
            return
        if not self.grid:
            return await update.effective_message.reply_text("网格引擎未初始化")
        lines = [f"🕸️ 网格状态 {self.env_tag}",
                 f"模式: {'网格' if self.grid_enabled else '单次低吸高卖'}"
                 f" | 层数 {self.grid_levels} | 间距 {self._cur_spacing()*100:.2f}%"]
        for sym in self.symbols:
            t = self.ws.get_ticker(sym)
            if not t:
                continue
            p = float(t.get('last') or 0)
            st = self.grid.stats(sym, p)
            if not st:
                lines.append(f"\n• {sym}: 未建网格")
                continue
            lines.append(
                f"\n• {sym} @ {p:.4f}\n"
                f"   档位 {st['lots']}/{self.grid_levels}"
                f" | 中枢 {st['center']:.4f} | 间距 {st['spacing_pct']:.2f}%\n"
                f"   已实现 {st['realized']:+.4f}U"
                f" | 浮动 {st['unrealized']:+.4f}U\n"
                f"   循环 {st['cycles']} 次 | 手续费 {st['fees']:.4f}U\n"
                f"   区间 {st['lower_band']:.4f} ~ {st['upper_band']:.4f}")
        return await update.effective_message.reply_text("\n".join(lines))

    async def cmd_gridreset(self, update, context):
        """/gridreset <币种> —— 重置该币种网格（不清仓，仅重建档位）"""
        if not self._auth(update):
            return
        args = context.args or []
        sym = (args[0].upper() if args else "")
        if "/" not in sym:
            sym = (sym or settings.SYMBOL) + "/USDT" if "/" not in (sym or settings.SYMBOL) else (sym or settings.SYMBOL)
        t = self.ws.get_ticker(sym)
        if not t:
            return await update.effective_message.reply_text(f"❌ 无行情 {sym}")
        p = float(t.get('last') or 0)
        self.grid.reset(sym, p)
        await self._save_runtime_state()
        return await update.effective_message.reply_text(f"✅ 已重置 {sym} 网格，锚点 {p:.4f}")

    def _cur_spacing(self):
        """当前生效的网格间距（供展示）"""
        try:
            return self.grid.calc_spacing(self._last_atr_pct)
        except Exception:
            return float(self.grid_spacing_pct)

    async def cmd_set_tf(self, update, context):
        """/settf 5m —— 设置 K 线周期"""
        if not self._auth(update):
            return
        args = context.args or []
        if not args:
            return await update.effective_message.reply_text("❌ 用法: /settf 5m")
        tf = args[0].strip().lower()
        if tf not in VALID_TIMEFRAMES:
            return await update.effective_message.reply_text(
                f"❌ 不支持周期 {tf}\n可选: {', '.join(sorted(VALID_TIMEFRAMES))}")
        self.timeframe = tf
        self._tech_cache.clear()
        self._tech_cache_time.clear()
        await self._save_config()
        return await update.effective_message.reply_text(f"✅ 周期: {self.timeframe}")

    # ---------- 币种校验 ----------

    # 稳定币：作为 base 时"低吸高卖"没有意义（价格恒定，赚不到差价）
    STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE"}

    def _validate_symbol(self, sym: str):
        """
        校验交易对是否可用于本策略。

        原实现 /addsymbol 完全不校验：
          1. 不存在的交易对会直接进入监控列表
          2. WebSocket 订阅遇到坏交易对会【整体失败】
             —— 一个坏币种拖垮所有币种的行情
          3. 主循环每轮都对它重试，持续刷错误日志

        返回 (ok: bool, reason: str)
        """
        if not sym or "/" not in sym:
            return False, "格式应为 BASE/QUOTE，如 ETH/USDT"

        parts = sym.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return False, "格式应为 BASE/QUOTE，如 ETH/USDT"

        base, quote = parts[0].upper(), parts[1].upper()

        # 合约/永续符号（如 ETH/USDT:USDT）
        if ":" in quote or ":" in base:
            return False, f"{sym} 是合约符号，本机器人只支持现货"

        # 稳定币当 base
        if base in self.STABLECOINS:
            return False, f"{base} 是稳定币，价格恒定，低吸高卖无意义"

        # 计价币种必须是 USDT（余额与下单都硬编码按 USDT 处理）
        if quote != "USDT":
            return False, (f"计价币种应为 USDT，当前是 {quote}\n"
                           f"余额与下单逻辑按 USDT 计价实现，用 {quote} 会导致金额判断错误")

        return True, ""

    async def _market_exists(self, sym: str):
        """
        查交易所是否真有这个交易对。 markets 未加载时跳过（宽松放行），
        网络异常也放行 —— 校验失败不该阻塞正常添加。
        """
        try:
            ex = getattr(self.exchange, "exchange", None) or self.exchange
            markets = getattr(ex, "markets", None)
            if not markets:
                return True, ""      # 无法校验，放行
            if sym in markets:
                m = markets[sym] or {}
                if m.get("active") is False:
                    return False, f"{sym} 在交易所已下线/不可交易"
                return True, ""
            return False, f"交易所在售列表中未找到 {sym}"
        except Exception as e:
            logger.debug(f"交易对存在性校验跳过: {e}")
            return True, ""

    def _watchdog_sample_data(self):
        """
        采样各币种行情新鲜度。

        目的：检测"订阅悄悄断了"。WebSocket 断开往往不报错，
        只是某个币再也收不到推送 —— 机器人会以为它一直没信号，
        实际是根本没数据。
        """
        try:
            wd = getattr(self, "watchdog", None)
            ws = getattr(self, "ws", None)
            if wd is None or ws is None:
                return
            for sym in self.symbols:
                t = ws.get_ticker(sym)
                if not t:
                    continue
                ts = float(t.get("timestamp") or 0)
                # 只有新鲜数据才算"收到了行情"
                if ts <= 0 or (time.time() - ts) <= float(self.data_max_age):
                    wd.record_data(sym)
        except Exception:
            pass

    def _purge_symbol_state(self, sym: str):
        """
        彻底清掉某币种的全部运行时状态。

        原 cmd_del_symbol 只清了 grid，其余六个容器全部残留：
            entries / position_lots / position_counts /
            _trailing_high / entry_details / _adaptive / coin_configs
        这些会随 _save_runtime_state 持久化到数据库，
        反复增删币种会让 DB 里堆积无效数据；
        若同名币种重新添加，还可能读到【上一次的旧状态】
        （例如旧的持仓成本、旧的自适应参数）。
        """
        removed = []
        for attr in ("entries", "position_lots", "position_counts",
                     "_trailing_high", "entry_details",
                     "_adaptive", "coin_configs"):
            d = getattr(self, attr, None)
            if isinstance(d, dict) and sym in d:
                d.pop(sym, None)
                removed.append(attr)
        if removed:
            logger.info(f"🧹 已清理 {sym} 的运行时状态: {', '.join(removed)}")
        return removed

    async def cmd_add_symbol(self, update, context):
        """/addsymbol ETH —— 新增监控币种"""
        if not self._auth(update):
            return
        args = context.args or []
        if not args:
            return await update.effective_message.reply_text("❌ 用法: /addsymbol ETH")
        sym = args[0].strip().upper()
        if "/" not in sym:
            sym += "/USDT"
        if sym in self.symbols:
            return await update.effective_message.reply_text("⚠️ 已存在")

        # 格式与计价币种校验
        ok, reason = self._validate_symbol(sym)
        if not ok:
            return await update.effective_message.reply_text(f"❌ {reason}")

        # 交易所是否真有这个交易对
        exists, why = await self._market_exists(sym)
        if not exists:
            return await update.effective_message.reply_text(
                f"❌ {why}\n\n💡 请检查拼写，或在交易所确认该交易对是否已上线")

        if len(self.symbols) >= MAX_SYMBOLS:
            return await update.effective_message.reply_text(
                f"⚠️ 最多 {MAX_SYMBOLS} 个币种（过多会拖慢轮询与 WebSocket）")
        self.symbols.append(sym)
        await self._resubscribe_symbols()
        await self._save_config()
        return await update.effective_message.reply_text(f"✅ 已添加 {sym}")

    async def cmd_del_symbol(self, update, context):
        """/delsymbol ETH —— 删除监控币种"""
        if not self._auth(update):
            return
        args = context.args or []
        if not args:
            return await update.effective_message.reply_text("❌ 用法: /delsymbol ETH")
        sym = args[0].strip().upper()
        if "/" not in sym:
            sym += "/USDT"
        if sym not in self.symbols:
            return await update.effective_message.reply_text("⚠️ 不存在")
        # 网格或持仓未清时拒绝删除，避免资金遗留
        gst = self.grid.get_state(sym) if self.grid else None
        if gst and gst.lots:
            return await update.effective_message.reply_text(
                f"⛔ {sym} 网格尚有 {len(gst.lots)} 档持仓，请先清仓")
        if self._bot_position_amount(sym) > 1e-12:
            return await update.effective_message.reply_text(
                f"⛔ {sym} 尚有持仓，请先平仓再删除")
        self.symbols.remove(sym)
        if self.grid:
            self.grid.remove(sym)
        self._purge_symbol_state(sym)
        await self._resubscribe_symbols()
        await self._save_config()
        await self._save_runtime_state()
        return await update.effective_message.reply_text(
            f"✅ 已删除 {sym}（含其运行时状态）")

    async def _resubscribe_symbols(self):
        """币种变动后重建 WebSocket 订阅"""
        try:
            asyncio.create_task(self.ws.watch_orderbooks(self.symbols))
        except Exception as e:
            logger.warning(f"重订阅订单簿失败: {e}")

    async def cmd_stats(self, update, context):
        """/stats —— 交易统计仪表盘"""
        if not self._auth(update):
            return
        perf = await get_recent_performance(20)
        today = await get_today_trades()
        lines = [
            f"📊 仪表盘 {self.env_tag}",
            f"模式: {'🕸️ 网格' if self.grid_enabled else '📈 单次低吸高卖'}",
            (f"今日: {today['total']}笔 胜率{today['win_rate']*100:.0f}% "
             f"盈亏{today['total_pnl_sum']:+.2f}%" if today else "今日无交易"),
            (f"近20笔: 胜率{perf['win_rate']*100:.0f}% 平均盈利{perf['avg_win_pct']:.2f}% "
             f"平均亏损{perf['avg_loss_pct']:.2f}%" if perf else "暂无数据"),
            f"总手续费: {await get_total_fees():.4f}U",
            f"总净利: {await get_total_net_profit():.4f}U",
            f"连续亏损: {self.risk.consecutive_losses}",
            f"回撤: {self.risk.last_drawdown*100:.2f}% / 上限{self.max_drawdown_pct*100:.0f}%",
            f"状态: {'⛔ 暂停' if self.risk.is_paused else '✅ 正常'}",
            # 新增三项保护的状态，让用户一眼看到它们在不在工作
            self.price_guard.summary(),
            self.retirement.summary(),
        ]
        if self.grid_enabled:
            lines.append("")
            lines.append("🕸️ 网格明细")
            for sym in self.symbols:
                t = self.ws.get_ticker(sym)
                if not t:
                    continue
                st = self.grid.stats(sym, float(t.get('last') or 0))
                if st:
                    lines.append(
                        f"  {sym}: 循环{st['cycles']}次 已实现{st['realized']:+.4f}U "
                        f"浮动{st['unrealized']:+.4f}U")
        return await update.effective_message.reply_text("\n".join(lines))

    async def cmd_backup(self, update, context):
        """/backup —— 导出数据库备份"""
        if not self._auth(update):
            return
        data = await export_db_to_json()
        if data:
            return await update.effective_message.reply_document(
                document=data.encode(),
                filename=f"backup_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}.json")
        return await update.effective_message.reply_text("备份失败")

    async def cmd_resume(self, update, context):
        """/resume —— 手动解除所有熔断并重置回撤峰值"""
        if not self._auth(update):
            return
        self.risk.resume()
        # 同时解除启动对账造成的暂停（人工确认后）
        reconciled = len(self.reconciler.blocked)
        if reconciled:
            self.reconciler.clear()
        # 解除退役线（这是唯一的人工恢复途径 ——
        # 退役线刻意不自动恢复，自动恢复等于没有底线）
        was_retired = self.retirement.retired
        if was_retired:
            self.retirement.reset()
        await self._save_runtime_state()
        extra = f"，并解除 {reconciled} 个币种的对账暂停" if reconciled else ""
        if was_retired:
            extra += "，并解除策略退役状态"
        return await update.effective_message.reply_text(
            f"✅ 已解除熔断，回撤峰值已重置{extra}")

    async def cmd_help(self, update, context):
        if not self._auth(update): return
        return await update.effective_message.reply_text(
            "📖 命令列表\n\n"
            "【基础】\n"
            "/menu 控制台  /status 状态  /stats 统计\n"
            "/check 信号  /holdings 持币  /panic 全平\n"
            "/history 交易记录  /brain 自检(查为什么不交易)\n"
            "/analysis 差距分析(手续费/胜率/期望)\n"
            "/preset 一键预设(small|standard|safe)\n"
            "/patrol 行为巡检（查是不是静默不干活）\n"
            "/autotrade on|off  /backup 备份  /resume 解除熔断\n"
            "/reconcile 持仓对账（比对交易所余额与本地账本）\n"
            "/restore 从备份恢复（把备份文件发给机器人回复本命令）\n"
            "/resetledger 清空本地账本（模拟盘转实盘前必做）\n\n"
            "【参数】—— 所有参数都能改\n"
            "/params 查看全部可调参数\n"
            "/set <参数名> <值>   例: /set grid_levels 10\n"
            "/get <参数名>        查看当前值与范围\n\n"
            "【网格】\n"
            "/grid 网格状态  /gridreset ETH 重置网格\n"
            "/set grid_enabled true  切换网格模式\n\n"
            "【币种】\n"
            "/addsymbol ETH  /delsymbol ETH  /settf 5m\n\n"
            "💡 旧命令 /settp /setsl /setmaxdd 等仍可用，等价转发到 /set"
        )


    # ---------- 诊断与分析 ----------

    async def cmd_brain(self, update, context):
        """
        /brain —— 系统自检：逐项检查并列出"为什么不交易"。

        小额测试时最常见的问题是"机器人开着但一单不下"，
        而原因分散在七八个地方（余额底线、网格最小额、数据陈旧、熔断…），
        逐个查要翻半天日志。这里一次性全列出来。
        """
        if not self._auth(update):
            return
        import time as _t
        lines = [f"🧠 大脑自检 {self.env_tag}", ""]

        ok_all = True

        # 1) 运行开关
        on = bool(self.is_running) and bool(self.auto_trade_enabled)
        lines.append(f"{'✅' if on else '❌'} 交易开关: "
                     f"运行={self.is_running} 自动={self.auto_trade_enabled}")
        if not on:
            ok_all = False
            lines.append("     → /autotrade on 开启")

        # 2) 行情数据新鲜度
        stale = []
        for sym in self.symbols:
            t = self.ws.get_ticker(sym) if self.ws else None
            if not t:
                stale.append(f"{sym}(无数据)")
                continue
            ts = float(t.get('timestamp') or 0)
            if ts > 0 and (_t.time() - ts) > float(self.data_max_age):
                stale.append(f"{sym}({_t.time()-ts:.0f}s前)")
        if stale:
            ok_all = False
            lines.append(f"❌ 行情陈旧: {', '.join(stale)}（阈值 {self.data_max_age}s）")
        else:
            lines.append(f"✅ 行情新鲜: {len(self.symbols)} 个币种")

        # 3) 余额 vs 保留底线
        try:
            bal = await self.exchange.fetch_balance()
            usdt = float(bal.get('USDT', {}).get('free', 0))
        except Exception:
            usdt = -1
        if usdt >= 0:
            okb = usdt >= float(self.reserve_bottom)
            lines.append(f"{'✅' if okb else '❌'} 可用 {usdt:.2f}U / 底线 {self.reserve_bottom}U")
            if not okb:
                ok_all = False
                lines.append(f"     → /setreserve 调低，或充值")
                lines.append("       9U 账户建议 /setreserve 1")

        # 4) 网格模式下的每格金额 vs 最小额
        if self.grid_enabled:
            capital = float(usdt if usdt > 0 else 0) * float(self.grid_capital_pct)
            per = capital / max(1, int(self.grid_levels))
            okp = per >= float(self.grid_min_order_usdt)
            lines.append(f"{'✅' if okp else '❌'} 网格每格 {per:.2f}U / 下限 {self.grid_min_order_usdt}U")
            if not okp:
                ok_all = False
                lines.append(f"     → 每格低于下限会全部跳过！")
                lines.append(f"       /setminorder 1 或 /setlevels 减少层数")
        else:
            lines.append("ℹ️ 单次模式（/set grid_enabled true 切网格）")

        # 5) 风控状态
        if self.risk.is_paused:
            ok_all = False
            lines.append(f"❌ 风控已熔断: {getattr(self.risk,'pause_reason','未知原因')}")
            lines.append("     → 确认仓位后 /resume 解除")
        else:
            lines.append("✅ 风控正常")

        # 6) 对账阻塞
        blocked = sorted(getattr(self.reconciler, "blocked", []) or [])
        if blocked:
            ok_all = False
            lines.append(f"❌ 对账阻塞: {', '.join(blocked)}")
            lines.append("     → 核对真实仓位后 /resume")
        else:
            lines.append("✅ 对账正常")

        # 7) 今日额度
        if self.max_daily_trades > 0 and self.daily_trades >= self.max_daily_trades:
            ok_all = False
            lines.append(f"❌ 今日已达上限 {self.daily_trades}/{self.max_daily_trades}")
        else:
            lines.append(f"✅ 今日 {self.daily_trades}/"
                         f"{self.max_daily_trades if self.max_daily_trades>0 else '∞'} 笔")

        lines.append("")
        if ok_all:
            lines.append("🎉 全部就绪，机器人应正常交易")
        else:
            lines.append("⚠️ 上面标 ❌ 的项会阻止交易，逐条处理即可")
        return await update.effective_message.reply_text("\n".join(lines))

    async def cmd_analysis(self, update, context):
        """
        /analysis —— 差距分析：理论 vs 实际，找出钱漏在哪。

        回答两个问题：
          1. 手续费吃掉了多少利润（毛利润 vs 净利润的差距）
          2. 信号评分到底有没有用（赢的分数 vs 亏的分数）
        """
        if not self._auth(update):
            return
        from storage import load_trades, get_recent_performance, get_total_fees
        perf = await get_recent_performance(50)
        fees = await get_total_fees()
        lines = [f"📐 差距分析 {self.env_tag}", ""]

        if not perf:
            lines.append("暂无成交数据，先跑几笔再来看")
            lines.append("")
            lines.append("💡 若一直没成交，用 /brain 查原因")
            return await update.effective_message.reply_text("\n".join(lines))

        pnls = perf.get('pnls') or []
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross = sum(pnls)

        lines.append(f"样本: {perf['total']} 笔（赢 {len(wins)} / 亏 {len(losses)}）")
        lines.append(f"胜率: {perf['win_rate']*100:.0f}%")
        lines.append(f"平均盈利: {perf['avg_win_pct']:+.3f}%")
        lines.append(f"平均亏损: {perf['avg_loss_pct']:+.3f}%")
        lines.append("")

        # 盈亏比与期望
        if wins and losses:
            rr = abs(perf['avg_win_pct'] / perf['avg_loss_pct'])
            exp = (perf['win_rate'] * perf['avg_win_pct']
                   + (1 - perf['win_rate']) * perf['avg_loss_pct'])
            lines.append(f"盈亏比: {rr:.2f} : 1")
            lines.append(f"单笔期望: {exp:+.4f}%")
            if exp > 0:
                lines.append("  ✅ 期望为正，策略逻辑站得住")
            else:
                lines.append("  ❌ 期望为负 —— 继续跑只会稳定亏损")
                lines.append("     → 放宽止盈或收紧止损再观察")
            lines.append("")

        # 手续费拖累
        lines.append(f"累计手续费: {fees:.4f}U")
        trades = await load_trades(50)
        gross_sum = sum(float(t.get('pnl_pct') or 0) for t in trades)
        net_sum = sum(float(t.get('net_pnl_pct') or 0) for t in trades)
        if abs(gross_sum) > 1e-9:
            drag = (gross_sum - net_sum) / abs(gross_sum) * 100
            lines.append(f"毛利 {gross_sum:+.3f}% → 净利 {net_sum:+.3f}%")
            lines.append(f"手续费拖累: {drag:.1f}%")
            if drag > 30:
                lines.append("  ⚠️ 手续费吃掉三成以上利润，间距太小了")
                lines.append(f"     → 当前间距 {self.grid_spacing_pct*100:.2f}%，"
                             f"建议 /setspacing 2 以上")

        return await update.effective_message.reply_text("\n".join(lines))

    async def cmd_history(self, update, context):
        """/history —— 最近交易记录"""
        if not self._auth(update):
            return
        from storage import load_trades
        n = 10
        args = context.args or []
        if args:
            try:
                n = max(1, min(50, int(args[0])))
            except ValueError:
                pass
        trades = await load_trades(n)
        if not trades:
            return await update.effective_message.reply_text(
                "📋 暂无交易记录\n\n💡 若机器人一直没成交，用 /brain 查原因")

        lines = [f"📋 最近 {len(trades)} 笔", ""]
        for t in trades:
            pnl = float(t.get('net_pnl_pct') or t.get('pnl_pct') or 0)
            mark = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            lines.append(
                f"{mark} {t.get('symbol','?')} "
                f"{t.get('entry',0):.4g}→{t.get('exit',0):.4g} "
                f"{pnl:+.2f}%  {t.get('time','')}")
        return await update.effective_message.reply_text("\n".join(lines))

    def _bt_kwargs(self):
        """
        把 bot 当前参数转换成回测配置，供 /backtest 使用。

        ⚠️ 曾经在这里踩过大坑：直接写 self.equity_cap，
        而真实参数名是 equity_cap_usdt —— 少了个后缀。
        QuantBot 没有 __getattr__ 兜底，直接抛
            AttributeError: 'QuantBot' object has no attribute 'equity_cap'
        结果三个币种的回测全部失败。

        更值得警惕的是：同文件另外两处（_effective_equity、
        网格仓位计算）早就用了 getattr(..., 'equity_cap_usdt', 0)
        安全访问，只有我新写的这行用了裸属性访问。

        现在改为：
          1. 全部走 getattr 兜底，缺省值取自 params.PARAMS 注册表
          2. 参数名写错时不崩溃，但记录告警日志
          3. 参数表改名自动同步，无需改这里
        """
        from core.params import PARAMS

        def g(key, cast=None):
            spec = PARAMS.get(key)
            default = spec.default if spec is not None else None
            if not hasattr(self, key):
                if spec is not None:
                    logger.warning(
                        f"⚠️ 回测参数缺失: {key}，回退默认值 {default}")
                return default
            v = getattr(self, key)
            if cast and v is not None:
                try:
                    return cast(v)
                except (TypeError, ValueError):
                    return default
            return v

        # 本金：优先用权益上限（若设置），否则用实际可用余额
        cap = g("equity_cap_usdt", float) or 0.0
        free = float(getattr(self, "_cached_usdt_free", 0) or 0)
        initial = cap if cap > 0 else free
        if initial <= 0:
            initial = 9.0

        return dict(
            initial_cash=max(9.0, float(initial)),
            order_type="limit" if g("grid_enabled") else "market",
            grid_enabled=bool(g("grid_enabled", bool)),
            grid_levels=int(g("grid_levels", int) or 1),
            grid_spacing_pct=float(g("grid_spacing_pct", float) or 0),
            grid_spacing_mode=str(g("grid_spacing_mode") or "fixed"),
            grid_capital_pct=float(g("grid_capital_pct", float) or 0),
            grid_min_order_usdt=float(
                g("grid_min_order_usdt", float) or 0),
            grid_stop_loss_pct=float(
                g("grid_stop_loss_pct", float) or 0),
            tp_pct=float(g("tp_pct", float) or 0),
            sl_pct=float(g("sl_pct", float) or 0),
            reserve_bottom=float(g("reserve_bottom", float) or 0),
            max_drawdown_pct=float(g("max_drawdown_pct", float) or 0),
            max_positions_per_coin=int(
                g("max_positions_per_coin", int) or 1),
            max_per_coin_usdt=float(g("max_per_coin_usdt", float) or 0),
        )

    async def cmd_backtest(self, update, context):
        """
        /backtest [币种] [天数] —— 用真实历史行情回测当前参数。

        为什么做成 Telegram 命令而不是本地脚本：
          1. 开发沙盒访问不了境外交易所（OKX/Binance 全部 403）
          2. 手机上跑不了命令行
          3. 服务器能连交易所，结果直接发回手机
        三者叠加，这是唯一可行的路径。

        报告不只看总收益，还会分段检验 ——
        避免"整段行情恰好上涨，任何策略都显得不错"的假象。
        """
        if not self._auth(update):
            return
        args = list(context.args or [])
        sym = args[0].upper() if args else (self.symbols[0]
                                            if self.symbols else "ETH/USDT")
        if "/" not in sym:
            sym += "/USDT"
        days = 30
        if len(args) > 1:
            try:
                days = max(3, min(365, int(args[1])))
            except ValueError:
                pass

        msg = update.effective_message
        await msg.reply_text(
            "🔬 正在回测 " + sym + "（近 " + str(days) + " 天）…\n"
            "   拉取真实 K 线，请稍候")

        try:
            from core.backtest import (BTConfig, Backtester, fetch_bars,
                                       run_regime_report,
                                       min_samples_for_significance,
                                       _new_public_exchange)
            tf = self.timeframe
            # 15m K 线：一天 96 根
            per_day = 96 if tf == "15m" else (24 if tf == "1h" else 96)
            limit = min(1500, days * per_day)
            # 用独立的公开实例拉行情 —— 沙盒模式下主 exchange
            # 可能返回模拟行情，那样整个回测就是建立在假数据上
            pub = _new_public_exchange()
            bars = await fetch_bars(self.exchange, sym, tf, limit,
                                    public_exchange=pub)
        except Exception as e:
            return await msg.reply_text(f"❌ 回测失败：{e}")

        if len(bars) < 200:
            return await msg.reply_text(
                f"❌ K 线不足（仅 {len(bars)} 根），至少需要 200 根")

        def make_cfg():
            return BTConfig(**self._bt_kwargs())

        try:
            full = Backtester(make_cfg()).run(bars)
            segs = run_regime_report(bars, make_cfg)
        except Exception as e:
            return await msg.reply_text(f"❌ 回测执行失败：{e}")

        need = min_samples_for_significance(0.55, 0.018, 0.010, 0.95)

        lines = [
            f"🔬 回测报告 {sym}",
            f"周期 {tf} | {len(bars)} 根 K 线 | 本金 {full.initial_cash:.2f}U",
        ]

        # 数据真实性校验 —— 防止沙盒返回模拟行情导致回测失真
        # 拿回测最后一根 K 线的价格，与机器人实时看到的价格比对
        try:
            tk = self.ws.get_ticker(sym) if getattr(self, "ws", None) \
                else None
            live = float((tk or {}).get("last") or 0)
            if live > 0 and bars:
                last_bt = bars[-1].close
                drift = abs(last_bt - live) / live
                if drift < 0.05:
                    pass            # 一致，不额外占用报告篇幅
                else:
                    lines.append(
                        f"⚠️ 数据源存疑：回测末价 {last_bt:.4f} "
                        f"vs 实时 {live:.4f}（差 {drift*100:.1f}%）")
                    lines.append(
                        "   可能是沙盒模拟行情，结论仅供参考")
        except Exception:
            pass

        lines += [
            f"交易 {full.n_trades} 笔 | 胜率 {full.win_rate*100:.0f}%",
            f"净利 {full.net_pnl:+.4f} U（{full.roi*100:+.2f}%）",
            f"最大回撤 {full.max_drawdown*100:.1f}%",
            f"手续费 {full.total_fee:.4f} U",
        ]
        if full.n_trades:
            lines.append(f"单笔期望 {full.expectancy:+.4f} U")

        # 样本量判定 —— 回答"是运气还是实力"
        lines.append("")
        if full.n_trades < (need or 62):
            lines.append(
                f"⚠️ 样本不足：{full.n_trades} 笔 < 建议 {need or 62} 笔")
            lines.append("   当前结果【不能】判定策略是否有效")
        else:
            lines.append(f"✅ 样本充足：{full.n_trades} 笔 ≥ {need} 笔")

        # 分段检验 —— 回答"收益来自策略还是行情"
        if segs:
            lines.append("")
            lines.append("分段检验：")
            for sg in segs:
                lines.append(
                    f"  {sg['label']} {sg['change']*100:+6.1f}% → "
                    f"{sg['n_trades']:>3}笔 "
                    f"{sg['net_pnl']:+.3f}U")

            # 幸存者偏差警示
            losing = [x for x in segs if x["net_pnl"] < 0]
            if len(losing) == len(segs) and len(segs) > 1:
                lines.append("")
                lines.append("🚨 所有行情段都亏损 —— 策略本身可能不成立")

        lines.append("")
        lines.append("—— 回测为历史模拟，不代表未来收益 ——")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n…(已截断)"
        return await msg.reply_text(text)

    async def cmd_patrol(self, update, context):
        """
        /patrol —— 立即跑一次行为巡检。

        机器人最大的风险不是崩溃，是【静默地不干活】：
        崩溃你立刻知道，装死你能看三天。
        本命令让你随时主动问一句"你是不是有什么不对劲"。
        """
        if not self._auth(update):
            return
        try:
            alerts = await self.watchdog.check_all()
            from storage import get_performance_windows
            w = await get_performance_windows(self.trend.RECENT_N,
                                              self.trend.BASELINE_N)
            alerts += await self.trend.check(w)
        except Exception as e:
            return await update.effective_message.reply_text(
                f"❌ 巡检失败: {e}")

        lines = ["🔭 行为巡检", ""]
        lines.append(f"状态: {self.watchdog.summary()}")
        lines.append("")
        if alerts:
            lines.append("⚠️ 发现以下异常：")
            lines.append("")
            for a in alerts:
                lines.append(a)
                lines.append("")
        else:
            lines.append("✅ 未发现异常")
            lines.append("")
            lines.append("以下情况会被检测到：")
            lines.append("  · 长时间未开仓（区分没信号 / 循环卡死）")
            lines.append("  · 网格挂单长期不成交")
            lines.append("  · 某币种长时间无行情（订阅断开）")
            lines.append("  · 手续费占比持续上升（间距被侵蚀）")
            lines.append("  · 胜率/单笔净利下滑（行情特征改变）")
        try:
            tr = self.trend.summary()
            if tr and tr != "尚未采样":
                lines.append("")
                lines.append(f"绩效: {tr}")
        except Exception:
            pass
        return await update.effective_message.reply_text("\n".join(lines))

    async def cmd_preset(self, update, context):
        """
        /preset —— 一键预设，避免逐条调参。

        小额账户最容易踩的坑就是默认值全是大资金设定的
        （reserve_bottom=10、grid_min_order_usdt=5），
        9U 本金下一单都挂不出来。预设一次性改对。
        """
        if not self._auth(update):
            return
        args = context.args or []
        name = (args[0].lower() if args else "")

        PRESETS = {
            "small": {   # 小额试水（≤50U）
                "reserve_bottom": 1.0,
                "grid_min_order_usdt": 1.0,
                "grid_levels": 1,
                "grid_spacing_pct": 0.02,
                "grid_capital_pct": 0.8,
                "single_order_pct": 0.02,
                "max_daily_trades": 20,
                "_desc": "小额试水（≤50U）：底线1U、单格最少1U、1层、间距2%",
            },
            "standard": {  # 标准（几百 U）
                # ⚠️ 原为 levels=8 / spacing=1.2%，回测证明是次优配置。
                # 混合行情（震荡+上涨+下跌，5 seed）风险调整收益：
                #   1层/1.5%  +30.4% 回撤17.2%  Calmar 1.77
                #   2层/2.0%  +30.9% 回撤17.7%  Calmar 1.74  ← 选它
                #   8层/1.2%  +22.7% 回撤14.8%  Calmar 1.54  ← 原默认
                # 层数越多每格越小、资金越分散，收益率反而越低。
                # 选 2 层而非 1 层：Calmar 几乎持平，但资金更分散。
                "reserve_bottom": 10.0,
                "grid_min_order_usdt": 5.0,
                "grid_levels": 2,
                "grid_spacing_pct": 0.020,
                "grid_capital_pct": 0.8,
                "single_order_pct": 0.02,
                "max_daily_trades": 20,
                "_desc": "标准（数百U）：恢复出厂默认",
            },
            "safe": {  # 保守：大间距、少交易
                "reserve_bottom": 1.0,
                "grid_min_order_usdt": 1.0,
                "grid_levels": 2,
                "grid_spacing_pct": 0.03,
                "grid_capital_pct": 0.5,
                "single_order_pct": 0.01,
                "max_daily_trades": 10,
                "_desc": "保守：间距3%、只用5成资金、每日最多10笔",
            },
        }

        if name not in PRESETS:
            return await update.effective_message.reply_text(
                "🎛️ 一键预设\n\n"
                "/preset small     小额试水（≤50U）★推荐 9U 账户\n"
                "/preset standard  标准（数百 U）\n"
                "/preset safe      保守（大间距、低仓位）\n\n"
                "⚠️ 会覆盖上面列出的参数，其他参数不变")

        p = PRESETS[name]
        desc = p.pop("_desc", "")
        changed = []
        for k, v in p.items():
            old = getattr(self, k, None)
            setattr(self, k, v)
            changed.append(f"  {k}: {old} → {v}")
        await self._save_config()

        return await update.effective_message.reply_text(
            f"✅ 已应用预设 {name}\n{desc}\n\n" + "\n".join(changed)
            + "\n\n💡 /params 查看全部，/brain 检查是否就绪")


    # ---------- 按钮处理 ----------
    async def handle_button_click(self, update, context):
        query = update.callback_query
        data = query.data

        # 原实现此处完全没有鉴权，任何能看到消息的人都可触发交易开关
        if not self._auth(update):
            await query.answer("⛔ 无权限", show_alert=True)
            return

        answered = False
        if data == "panic_confirm":
            # 原实现只提示"发送 /panic 确认"，用户还得手动再打一遍命令，
            # 面板按钮形同虚设。改为点一次弹确认、再点一次才真执行，
            # 既保证二次确认的安全性，又不用手打命令。
            if not context.user_data.get("panic_armed"):
                context.user_data["panic_armed"] = True
                await query.answer(
                    "⚠️ 再点一次「紧急全平」确认执行全部清仓", show_alert=True)
            else:
                context.user_data["panic_armed"] = False
                await query.answer("🚨 执行中…", show_alert=True)
                await self.cmd_panic(update, context)
            answered = True
        elif data == "toggle_auto":
            self.auto_trade_enabled = not self.auto_trade_enabled
            await self._save_config()
            await query.answer(f"自动交易已{'开启' if self.auto_trade_enabled else '关闭'}")
            await self._refresh_panel(query)
        elif data == "bot_start":
            self.is_running = True
            await query.answer("已开启")
        elif data == "bot_stop":
            self.is_running = False
            await query.answer("已关机")
        elif data == "refresh":
            await self._refresh_panel(query)
            answered = True
        elif data == "status":
            await self.cmd_status(update, context)
        elif data == "holdings":
            await self.cmd_holdings(update, context)
        elif data == "check":
            await self.cmd_check(update, context)
        elif data == "balance":
            bal = await self.exchange.fetch_balance()
            await query.message.reply_text(f"USDT: {float(bal.get('USDT',{}).get('free',0)):.2f}")
        elif data == "stats":
            await self.cmd_stats(update, context)
        elif data == "backup":
            await self.cmd_backup(update, context)
        elif data.startswith("q:"):
            # 快捷值：q:<提示消息ID>:<值>，点一下直接应用，不用手输
            await self._handle_quick_value(query, context, data)
            answered = True
        elif data.startswith("qcancel:"):
            mid = data.split(":", 1)[1]
            pend_map = context.user_data.get("pending_by_msg") or {}
            pend_map.pop(int(mid), None) if str(mid).lstrip("-").isdigit() else None
            context.user_data.pop("pending", None)
            await query.answer("已取消")
            try:
                await query.edit_message_text("↩️ 已取消")
            except Exception:
                pass
            answered = True
        elif data in MENU_INPUT_SPEC:
            kind, key, label = MENU_INPUT_SPEC[data]
            await self._send_input_prompt(query, context, kind, key, label, data)
            answered = True
        elif data == "import_pos":
            blocked = sorted(getattr(self.reconciler, "blocked", set()) or set())
            if not blocked:
                await update.effective_message.reply_text(
                    "ℹ️ 当前没有币种被对账阻塞，无需导入。\n"
                    "若需导入其他币种：/import ETH/USDT")
                return
            await update.effective_message.reply_text(
                f"⏳ 正在导入 {len(blocked)} 个被阻塞币种的持仓…\n"
                f"   {', '.join(x.split('/')[0] for x in blocked)}")
            context.args = []
            await self.cmd_import_position(update, context)
            return
        elif data == "resume":
            self.risk.resume()
            await self._save_runtime_state()
            await query.answer("✅ 已手动解除熔断", show_alert=True)
            answered = True
        elif data == "grid":
            await self.cmd_gridstatus(update, context)
        elif data == "params":
            await self.cmd_params(update, context)
        elif data == "reconcile":
            await self.cmd_reconcile(update, context)
        elif data == "restore":
            await self.cmd_restore(update, context)
        else:
            # 兜底：不再笼统提示"功能待实现"，而是列出可用按钮便于排查
            await query.answer("未知操作，请用 /menu 刷新面板", show_alert=True)
            answered = True

        # 只应答一次；Telegram 对同一 callback_query 重复 answer 会报错
        if not answered:
            await query.answer()

    async def _refresh_panel(self, query):
        try:
            await query.edit_message_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())
        except:
            pass

    async def _send_input_prompt(self, query, context, kind, key, label, src_data):
        """
        弹出输入：先发一条带常用值按钮的提示，再发一条 ForceReply 锚点。

        ForceReply 是 Telegram 里最接近"弹窗输入"的机制 ——
        客户端会自动把输入焦点锁在这条消息上（输入框顶部显示"回复该消息"），
        用户打的字就是对它作答，不用自己去点输入框盲打。

        两条消息的原因：一条消息上不能同时挂 InlineKeyboard 和 ForceReply，
        所以按钮走提示消息、输入锚点走第二条；待办在两条 ID 下都注册，
        无论用户回复哪一条都能命中。
        """
        from core.params import PARAMS

        text = await self._pending_hint(kind, key, label)

        # 常用值
        if kind == "del_symbol":
            quick = [x.split("/")[0] for x in self.symbols][:6]
        elif kind == "add_symbol":
            quick = [c for c in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
                     if f"{c}/USDT" not in self.symbols][:6]
        else:
            quick = list(QUICK_VALUES.get(key if kind == "param" else kind, []) or [])

        sent = await query.message.reply_text("⏳")
        mid = sent.message_id

        rows = []
        if quick:
            row = []
            for v in quick:
                row.append(InlineKeyboardButton(v, callback_data=f"q:{mid}:{v}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        rows.append([InlineKeyboardButton("❌ 取消", callback_data=f"qcancel:{mid}")])

        await sent.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))

        # 输入栏灰字提示（键盘上方的 placeholder）
        if kind == "param" and key in PARAMS:
            unit = PARAMS[key].unit or ""
            ph = f"输入{label}" + (f"（{unit}）" if unit else "")
        elif kind == "timeframe":
            ph = "如 15m"
        elif kind in ("add_symbol", "del_symbol"):
            ph = "如 BTC"
        else:
            ph = "输入新值"

        anchor_msg = await sent.reply_text(
            "⬆️ 在输入框直接输入，或点上方按钮选择",
            reply_markup=ForceReply(selective=True,
                                    input_field_placeholder=ph[:64]),
        )

        # 两条消息都注册待办，回复任一条都能命中
        rec = {"kind": kind, "key": key, "label": label,
               "data": src_data, "msg_id": mid}
        pend = context.user_data.setdefault("pending_by_msg", {})
        pend[mid] = rec
        pend[anchor_msg.message_id] = rec
        # 兼容非 reply 的普通输入
        context.user_data["pending"] = rec

    async def _handle_quick_value(self, query, context, data):
        """快捷值按钮 q:<msg_id>:<值> —— 复用输入逻辑，校验规则保持一致"""
        parts = data.split(":", 2)
        if len(parts) < 3:
            return await query.answer("数据格式错误", show_alert=True)
        mid, val = parts[1], parts[2]
        pend_map = (context.user_data or {}).get("pending_by_msg") or {}
        try:
            rec = pend_map.get(int(mid))
        except (ValueError, TypeError):
            rec = None
        if not rec:
            await query.answer("该输入已过期，请重新点按钮", show_alert=True)
            return

        # 构造最小 update 复用 handle_text_input，保证校验一致
        class _U:
            pass
        fake = _U()
        fake.effective_message = query.message
        fake.effective_user = query.from_user
        query.message.text = val
        query.message.reply_to_message = type(
            "M", (), {"message_id": int(mid)})()
        await self.handle_text_input(fake, context)
        await query.answer()

    async def _pending_hint(self, kind: str, key, label: str) -> str:
        """拼出交互式输入的提示语（含当前值、范围、可选项）"""
        from core.params import PARAMS, display, range_hint
        if kind == "param" and key in PARAMS:
            spec = PARAMS[key]
            cur = display(key, getattr(self, key, spec.default))
            return (f"✏️ 请输入新的{label}\n\n"
                    f"   当前: {cur}\n"
                    f"   范围: {range_hint(spec)}\n\n"
                    f"直接发送数字即可，输入 /cancel 取消")
        if kind == "timeframe":
            return (f"✏️ 请输入新的{label}\n\n"
                    f"   当前: {self.timeframe}\n"
                    f"   可选: {', '.join(sorted(VALID_TIMEFRAMES))}\n\n"
                    f"直接发送如 15m，输入 /cancel 取消")
        if kind == "add_symbol":
            return (f"✏️ 请输入{label}\n\n"
                    f"   当前: {', '.join(self.symbols)}\n"
                    f"   上限: {MAX_SYMBOLS} 个\n\n"
                    f"直接发送如 BTC（自动补 /USDT），输入 /cancel 取消")
        if kind == "del_symbol":
            return (f"✏️ 请输入{label}\n\n"
                    f"   当前: {', '.join(self.symbols)}\n\n"
                    f"直接发送如 BTC，输入 /cancel 取消")
        return f"✏️ 请输入新的{label}（输入 /cancel 取消）"

    async def handle_text_input(self, update, context):
        """
        处理面板按钮发起的交互式输入。

        原实现是空的 pass —— 用户按提示输入后毫无反应，
        面板里 10 个按钮等于全部失效。这里补上消费逻辑。

        没有待办输入时静默忽略，不打扰用户。
        """
        msg = update.effective_message
        text = (msg.text or "").strip()
        if not text:
            return

        # 精确匹配：用户是在"回复"哪一条提示？
        # ForceReply 会把用户的输入标记为 reply_to_message，
        # 据此取到对应待办 —— 这样可以同时挂多个待办而不串台。
        pend_map = (context.user_data or {}).get("pending_by_msg") or {}
        target_id = None
        rm = getattr(msg, "reply_to_message", None)
        if rm is not None:
            target_id = getattr(rm, "message_id", None)

        pending = pend_map.get(target_id) if target_id is not None else None
        # 回退：没有 reply 标记（比如手动输入）时用最后一个待办
        if pending is None:
            pending = (context.user_data or {}).get("pending")
        if not pending:
            return
        if not self._auth(update):
            return

        def _clear():
            """清理该待办（按 ID 精确清理，不影响其他待办）"""
            if target_id is not None:
                pend_map.pop(target_id, None)
            context.user_data.pop("pending", None)

        # 取消
        if text.lower() in ("/cancel", "cancel", "取消", "/取消"):
            _clear()
            return await msg.reply_text("↩️ 已取消输入")

        kind = pending.get("kind")
        key = pending.get("key")

        try:
            if kind == "param":
                from core.params import PARAMS, parse, display, range_hint
                val, err = parse(key, text)
                if err:
                    spec = PARAMS[key]
                    return await msg.reply_text(
                        f"❌ {err}\n范围: {range_hint(spec)}\n\n"
                        f"请重新输入，或发送 /cancel 取消")
                old = getattr(self, key, None)
                setattr(self, key, val)
                await self._save_config()
                _clear()
                spec = PARAMS[key]
                return await msg.reply_text(
                    f"✅ {spec.desc}\n"
                    f"   {display(key, old)} → {display(key, val)}")

            if kind == "timeframe":
                tf = text.lower()
                if tf not in VALID_TIMEFRAMES:
                    return await msg.reply_text(
                        f"❌ 不支持周期 {tf}\n"
                        f"可选: {', '.join(sorted(VALID_TIMEFRAMES))}\n\n"
                        f"请重新输入，或发送 /cancel 取消")
                old = self.timeframe
                self.timeframe = tf
                self._tech_cache.clear()
                self._tech_cache_time.clear()
                await self._save_config()
                _clear()
                return await msg.reply_text(f"✅ 周期: {old} → {tf}")

            if kind in ("add_symbol", "del_symbol"):
                sym = text.strip().upper()
                if "/" not in sym:
                    sym += "/USDT"
                if kind == "add_symbol":
                    if sym in self.symbols:
                        return await msg.reply_text(f"⚠️ {sym} 已存在")
                    ok, reason = self._validate_symbol(sym)
                    if not ok:
                        return await msg.reply_text(f"❌ {reason}")
                    exists, why = await self._market_exists(sym)
                    if not exists:
                        return await msg.reply_text(
                            f"❌ {why}\n\n请重新输入，或发送 /cancel 取消")
                    if len(self.symbols) >= MAX_SYMBOLS:
                        return await msg.reply_text(
                            f"⚠️ 最多 {MAX_SYMBOLS} 个币种")
                    self.symbols.append(sym)
                    await self._resubscribe_symbols()
                    await self._save_config()
                    _clear()
                    return await msg.reply_text(f"✅ 已添加 {sym}")
                else:
                    if sym not in self.symbols:
                        return await msg.reply_text(f"⚠️ {sym} 不存在")
                    gst = self.grid.get_state(sym) if self.grid else None
                    if gst and gst.lots:
                        return await msg.reply_text(
                            f"⛔ {sym} 网格尚有 {len(gst.lots)} 档持仓，请先清仓")
                    if self._bot_position_amount(sym) > 1e-12:
                        return await msg.reply_text(
                            f"⛔ {sym} 尚有持仓，请先平仓再删除")
                    self.symbols.remove(sym)
                    if self.grid:
                        self.grid.remove(sym)
                    self._purge_symbol_state(sym)
                    await self._resubscribe_symbols()
                    await self._save_config()
                    await self._save_runtime_state()
                    _clear()
                    return await msg.reply_text(
                        f"✅ 已删除 {sym}（含其运行时状态）")
        except Exception as e:
            logger.error(f"交互式输入处理失败: {e}")
            return await msg.reply_text(f"❌ 处理失败: {e}")

        await msg.reply_text("❌ 未知的输入类型，已取消")
        _clear()

    # ---------- 主运行循环 ----------
    async def run(self):
        await self.load_and_init()
        if not self.tg_app:
            logger.error("Telegram bot token 未配置")
            return

        ws_ok = await self.ws.connect()
        if ws_ok:
            asyncio.create_task(self.ws.watch_tickers(self.symbols))
            asyncio.create_task(self.ws.watch_orderbooks(self.symbols))

        await self.tg_app.bot.delete_webhook(drop_pending_updates=True)

        # 登记后台任务并包一层监护：
        # task 静默死亡时，健康检查仍 200、Telegram 仍能响应，
        # 只有交易彻底停止 —— 这是最难发现的一类故障。
        for name, coro_fn in (("自动交易", self._auto_trade_monitor),
                              ("移动止盈止损", self._trailing_monitor),
                              ("风控监控", self._risk_monitor_task),
                              ("网格交易", self._grid_monitor)):
            self.watchdog.expect_task(name)
            asyncio.create_task(self._guard_task(name, coro_fn))

        # 启动对账：把持久化的网格状态与交易所实际挂单对齐。
        # 声明式执行的好处 —— 只需重算目标状态，差异会在下一轮 sync 自动补齐。
        if self.grid_enabled:
            restored = len(self.grid.states)
            logger.info(f"🕸️ 网格模式已启用，恢复 {restored} 个币种状态，"
                        f"将在首个同步周期与交易所对账")

        # 原实现在内层写了 'while True: await asyncio.sleep(30)'，
        # 导致外层 try/except 永远等不到异常 —— 断线后既不重连也不清理，静默假死。
        consecutive_failures = 0          # 连续失败次数，用于指数退避
        while True:
            wait = 5                       # 本次循环结束后的等待秒数
            try:
                self.mark_alive()          # 每次循环刷新心跳
                await self.tg_app.initialize()
                await self.tg_app.start()
                await self.tg_app.updater.start_polling(drop_pending_updates=True)
                logger.info("✅ UltimateBot 启动成功")
                if settings.TG_CHAT_ID:
                    await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,
                        text="🚀 **自适应低吸高卖引擎已启动**\n\n"
                             "⚡ 现货低吸高卖 + 市场自适应\n"
                             "🛡️ 趋势过滤 / 回撤熔断已启用\n"
                             "📌 发送 /autotrade on 启动交易")
                # 阻塞直到 updater 停止。
                #
                # PTB v20 已移除 Updater.idle()（那是 v13 的同步 API），
                # 直接调用会抛 'Updater' object has no attribute 'idle'。
                # 改为轮询 running 状态，并顺带检测后台 polling 任务是否意外结束：
                #   1) 正常停止 → running 变 False，退出循环走重连
                #   2) polling 崩溃 → 抛出真实异常，由外层 except 捕获后重连
                #   3) 每轮刷新心跳 —— 这一点很重要，否则长时间阻塞期间
                #      健康检查会因心跳不更新而误判为"假活"
                polling_task = getattr(self.tg_app.updater,
                                      "_Updater__polling_task", None)
                while getattr(self.tg_app.updater, "running", False):
                    if polling_task is not None and polling_task.done():
                        exc = (polling_task.exception()
                               if not polling_task.cancelled() else None)
                        if exc is not None:
                            raise exc      # 交给外层，触发重连
                        break
                    self.mark_alive()
                    await asyncio.sleep(1)
                consecutive_failures = 0  # 成功启动，重置退避计数
                down_for = self._note_tg_up()
                if down_for > 60:
                    logger.info(
                        f"✅ Telegram 已恢复（此前断连 {down_for/60:.1f} 分钟）")
                logger.warning("⚠️ Telegram updater 已停止，准备重连")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_failures += 1
                kind = self._classify_tg_error(e)

                if kind == "fatal":
                    # 永久性错误：token 失效 / 机器人被拉黑 / chat_id 填错。
                    # 重试一万次也不会成功。原实现不区分，
                    # 于是每 60 秒重试一次、永不停止，
                    # 而 Telegram 是唯一通知渠道 —— 用户完全不知情，
                    # 交易却还在跑，/panic 发不出去，持仓失控。
                    #
                    # 这里立刻大声报错（日志 + 健康检查），
                    # 给出明确的修复指引，然后放慢重试等待人工介入。
                    self._note_tg_down()
                    if not self._tg_fatal_reported:
                        self._tg_fatal_reported = True
                        logger.error(
                            "🚨 Telegram 连接发生【永久性错误】，重试无法恢复！\n"
                            f"   错误: {e}\n"
                            "   请检查：\n"
                            "     · TG_BOT_TOKEN 是否失效或被 revoke\n"
                            "     · 机器人是否被拉黑\n"
                            "     · TG_CHAT_ID 是否正确\n"
                            "   在 Render 面板修正后重新部署。")
                    wait = 300      # 5 分钟一次，避免刷日志
                elif kind == "conflict":
                    # 另一个实例正在 getUpdates（常见于 Render 滚动重启时
                    # 新旧实例短暂重叠）。此时狂试毫无意义 —— 只会让两边
                    # 互相踢对方，陷入死循环。必须退避，等旧实例彻底退出。
                    self._note_tg_down()
                    wait = min(30 * consecutive_failures, 180)
                    logger.warning(
                        f"⚠️ 检测到多实例冲突(Conflict)：另一个进程正在轮询。"
                        f"第 {consecutive_failures} 次，退避 {wait}s 后重试。"
                        f"若持续出现，请确认只有一个实例在运行。")
                else:
                    # 临时性错误（NetworkError / Bad Gateway / 超时等）。
                    # Telegram 服务端或网络抖动，通常几秒~几分钟自愈。
                    self._note_tg_down()
                    down_for = time.time() - (self._tg_down_since or time.time())
                    wait = min(5 * consecutive_failures, 60)
                    logger.error(
                        f"Bot 断开: {e}（第 {consecutive_failures} 次，"
                        f"{wait}s 后重连）")
                    # 断连超过 5 分钟要特别提示：此时用户已失去对
                    # 机器人的控制（命令发不进来、告警发不出去），
                    # 但交易仍在继续 —— 这是需要人知道的状态。
                    if down_for > 300 and int(down_for) % 300 < 6:
                        logger.error(
                            f"🚨 Telegram 已断连 {down_for/60:.0f} 分钟，"
                            f"命令与告警均无法收发，但交易仍在运行。"
                            f"如需紧急停止，请到 Render 面板手动 Suspend。")
            finally:
                # 无论正常停止还是异常，都彻底清理，避免反复 initialize 造成句柄泄漏
                await self._shutdown_telegram()

            # 退避等待；期间持续刷新心跳，避免被健康检查误判为假死
            for _ in range(int(wait)):
                self.mark_alive()
                await asyncio.sleep(1)

    async def _shutdown_telegram(self):
        """幂等关闭 Telegram 应用；任一步失败都不影响下一次重连。"""
        try:
            if self.tg_app.updater and self.tg_app.updater.running:
                await self.tg_app.updater.stop()
        except Exception as e:
            logger.debug(f"updater 停止异常(可忽略): {e}")
        try:
            if self.tg_app.running:
                await self.tg_app.stop()
        except Exception as e:
            logger.debug(f"app 停止异常(可忽略): {e}")
        try:
            await self.tg_app.shutdown()
        except Exception as e:
            logger.debug(f"app shutdown 异常(可忽略): {e}")

    # ---------- 风控状态代理 ----------
    # 历史代码直接读写 self._is_paused 等属性；改为代理到 RiskManager，
    # 使风控状态有单一归属，同时不必改动全部调用点。
    @property
    def _is_paused(self):
        return self.risk.is_paused

    @_is_paused.setter
    def _is_paused(self, v):
        self.risk.is_paused = bool(v)

    @property
    def _drawdown_safe_flag(self):
        return self.risk.drawdown_safe

    @_drawdown_safe_flag.setter
    def _drawdown_safe_flag(self, v):
        self.risk.drawdown_safe = bool(v)

    @property
    def _drawdown_alerted(self):
        return self.risk.drawdown_alerted

    @_drawdown_alerted.setter
    def _drawdown_alerted(self, v):
        self.risk.drawdown_alerted = bool(v)

    @property
    def _last_drawdown(self):
        return self.risk.last_drawdown

    @_last_drawdown.setter
    def _last_drawdown(self, v):
        self.risk.last_drawdown = float(v)

    @property
    def peak_total_value(self):
        return self.risk.peak_equity

    @peak_total_value.setter
    def peak_total_value(self, v):
        self.risk.peak_equity = float(v)

    @property
    def _consecutive_losses(self):
        return self.risk.consecutive_losses

    @_consecutive_losses.setter
    def _consecutive_losses(self, v):
        self.risk.consecutive_losses = int(v)

    @property
    def _last_pause_time(self):
        return self.risk.last_pause_time

    @_last_pause_time.setter
    def _last_pause_time(self, v):
        self.risk.last_pause_time = float(v)

    @property
    def _today_loss_pct(self):
        return self.risk.today_loss_pct

    @_today_loss_pct.setter
    def _today_loss_pct(self, v):
        self.risk.today_loss_pct = float(v)

    @property
    def _today_loss_usdt(self):
        return self.risk.today_loss_usdt

    @_today_loss_usdt.setter
    def _today_loss_usdt(self, v):
        self.risk.today_loss_usdt = float(v)

    @property
    def _daily_start_equity(self):
        return self.risk.daily_start_equity

    @_daily_start_equity.setter
    def _daily_start_equity(self, v):
        self.risk.daily_start_equity = float(v)

    @property
    def daily_trades(self):
        return self.risk.daily_trades

    @daily_trades.setter
    def daily_trades(self, v):
        self.risk.daily_trades = int(v)

        # 缓存
        self._cached_balances = {}
        self._cached_usdt_free = 0.0
        self._balance_cache_time = 0
        self._balance_cache_ttl = 15
        self._tech_cache = {}
        self._tech_cache_time = {}
        self._tech_cache_ttl = 30
        self._price_history = {}

        # AI 分析（简化）
        self.ai_insight = {"timestamp": 0, "summary": "等待分析", "recommendation": "观望", "score": 50}
        self.ai_enabled = False

        # 费率
        self.taker_fee = settings.TAKER_FEE
        self.maker_fee = settings.MAKER_FEE
        self.min_profit_margin = settings.MIN_PROFIT_MARGIN
        self.breakeven_pct = (self.taker_fee * 2) + self.min_profit_margin

        # Telegram 权限
        raw = settings.ALLOWED_USERS
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag = "🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"
        self.coin_configs = {}

        # ---------- 自适应参数（按币种隔离）----------
        self._adaptive = {}
        self._adaptive_state = 'neutral'
        self._adaptive_tp_factor = 1.0
        self._adaptive_sl_factor = 1.0
        self._adaptive_score_offset = 0
        self._adaptive_amount_factor = 1.0
        self._adaptive_update_time = 0

        # Telegram 应用
        self.tg_app = None
        if settings.TG_BOT_TOKEN:
            self._init_telegram()

    def _init_telegram(self):
        self.tg_app = ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
        handlers = [
            CommandHandler("start", self.cmd_menu),
            CommandHandler("menu", self.cmd_menu),
            CommandHandler("status", self.cmd_status),
            CommandHandler("check", self.cmd_check),
            CommandHandler("holdings", self.cmd_holdings),
            CommandHandler("panic", self.cmd_panic),
            CommandHandler("autotrade", self.cmd_autotrade),
            CommandHandler("settf", self.cmd_set_tf),
            CommandHandler("addsymbol", self.cmd_add_symbol),
            CommandHandler("delsymbol", self.cmd_del_symbol),
            # 通用参数命令
            CommandHandler("set", self.cmd_set),
            CommandHandler("get", self.cmd_get),
            CommandHandler("params", self.cmd_params),
            # 网格
            CommandHandler("grid", self.cmd_gridstatus),
            CommandHandler("gridstatus", self.cmd_gridstatus),
            CommandHandler("gridreset", self.cmd_gridreset),
            CommandHandler("stats", self.cmd_stats),
            CommandHandler("backup", self.cmd_backup),
            CommandHandler("resume", self.cmd_resume),
            CommandHandler("reconcile", self.cmd_reconcile),
            CommandHandler("restore", self.cmd_restore),
            CommandHandler("import", self.cmd_import_position),
            CommandHandler("resetledger", self.cmd_resetledger),
            CommandHandler("help", self.cmd_help),
            CommandHandler("brain", self.cmd_brain),
            CommandHandler("analysis", self.cmd_analysis),
            CommandHandler("history", self.cmd_history),
            CommandHandler("preset", self.cmd_preset),
            CommandHandler("patrol", self.cmd_patrol),
            CommandHandler("backtest", self.cmd_backtest),
        ]
        for h in handlers:
            self.tg_app.add_handler(h)

        # 旧命令别名（/settp /setsl /setlevels …）统一转发到 /set，
        # 由 params 注册表的 ALIAS_MAP 声明，新增别名无需改动此处
        from core.params import ALIAS_MAP
        for alias in ALIAS_MAP:
            self.tg_app.add_handler(CommandHandler(alias, self.cmd_alias))
        self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
        self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))
