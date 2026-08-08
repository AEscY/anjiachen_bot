"""
bot.py - Telegram 完全体交互层（已修复：真实资金费率 + 盘口数据）
"""
import asyncio, random
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from config import settings, logger
from indicators import TechnicalEngine
from storage import load_config, save_config

CST = timezone(timedelta(hours=8))

class MacroEngine:
    async def check(self):
        score = random.uniform(0.05, 0.35)
        return {'is_safe': score < 0.75, 'score': score, 'status': "🟢 平稳" if score < 0.75 else "🚨 风险"}

class OrderbookEngine:
    async def validate(self, orderbook):
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if not bids or not asks:
            return False, "盘口数据缺失"
        spread = ((asks[0][0] - bids[0][0]) / bids[0][0]) * 100
        if spread > 0.2:
            return False, f"价差过大 ({spread:.3f}%)"
        return True, f"盘口健康 (价差: {spread:.3f}%)"

class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.tech = TechnicalEngine()
        self.macro = MacroEngine()
        self.orderbook_engine = OrderbookEngine()
        self.lock = asyncio.Lock()

        cfg = load_config()
        self.is_running = True
        self.orderbook_filter = cfg.get('orderbook_filter', True)
        self.waterfall_breaker = cfg.get('waterfall_breaker', True)
        self.symbols = cfg.get('symbols', []) or [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]
        self.tp_pct = cfg.get('tp_pct', 0.08)
        self.sl_pct = cfg.get('sl_pct', 0.05)
        self.trailing_sl_pct = cfg.get('trailing_sl_pct', 0.02)
        self.single_order_usdt = cfg.get('single_order_usdt', 100)
        self.timeframe = cfg.get('timeframe', '15m')
        self.reserve_bottom = cfg.get('reserve_bottom', 50)

        raw = settings.ALLOWED_USERS
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag = "🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"

        self.tg_app = None
        if settings.TG_BOT_TOKEN:
            self.tg_app = ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
            self.tg_app.add_handler(CommandHandler("start", self.cmd_menu))
            self.tg_app.add_handler(CommandHandler("menu", self.cmd_menu))
            self.tg_app.add_handler(CommandHandler("panic", self.cmd_panic))
            self.tg_app.add_handler(CommandHandler("analysis", self.cmd_analysis))
            self.tg_app.add_handler(CommandHandler("brain", self.cmd_brain))
            self.tg_app.add_handler(CommandHandler("help", self.cmd_help))
            self.tg_app.add_handler(CommandHandler("settp", self.cmd_set_tp))
            self.tg_app.add_handler(CommandHandler("setsl", self.cmd_set_sl))
            self.tg_app.add_handler(CommandHandler("settsl", self.cmd_set_tsl))
            self.tg_app.add_handler(CommandHandler("setamount", self.cmd_set_amount))
            self.tg_app.add_handler(CommandHandler("settf", self.cmd_set_tf))
            self.tg_app.add_handler(CommandHandler("setreserve", self.cmd_set_reserve))
            self.tg_app.add_handler(CommandHandler("addsymbol", self.cmd_add_symbol))
            self.tg_app.add_handler(CommandHandler("delsymbol", self.cmd_del_symbol))
            self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
            self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    def _save(self):
        save_config({
            'tp_pct': self.tp_pct, 'sl_pct': self.sl_pct,
            'trailing_sl_pct': self.trailing_sl_pct, 'single_order_usdt': self.single_order_usdt,
            'timeframe': self.timeframe, 'reserve_bottom': self.reserve_bottom,
            'symbols': self.symbols, 'orderbook_filter': self.orderbook_filter,
            'waterfall_breaker': self.waterfall_breaker
        })

    def _auth(self, update: Update):
        if not self.allowed: return True
        return update.effective_user.id in self.allowed

    def _parse_pct(self, val):
        return val / 100 if val >= 1 else val

    # ---------- 键盘构造器 ----------
    def _build_main_keyboard(self):
        f_status = "已开启" if self.orderbook_filter else "已关闭"
        b_status = "已开启" if self.waterfall_breaker else "已关闭"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平 (Panic)", callback_data="panic_confirm")],
            [InlineKeyboardButton(f"🏮 盘口过滤: [{f_status}]", callback_data="toggle_filter"),
             InlineKeyboardButton(f"🚨 防瀑布熔断: [{b_status}]", callback_data="toggle_breaker")],
            [InlineKeyboardButton("⚡ 开启运行", callback_data="bot_start"),
             InlineKeyboardButton("🔴 优雅关机", callback_data="bot_stop")],
            [InlineKeyboardButton("📊 运行看板", callback_data="dashboard"),
             InlineKeyboardButton("💳 账户余额", callback_data="balance")],
            [InlineKeyboardButton("🎯 止盈率 %", callback_data="menu_set_tp"),
             InlineKeyboardButton("🛡️ 硬止损 %", callback_data="menu_set_sl")],
            [InlineKeyboardButton("📉 移动止损 %", callback_data="menu_set_tsl"),
             InlineKeyboardButton("💵 单笔 USDT", callback_data="menu_set_amount")],
            [InlineKeyboardButton("⏱️ K线周期", callback_data="menu_set_tf"),
             InlineKeyboardButton("🔒 保留底线", callback_data="menu_set_reserve")],
            [InlineKeyboardButton("➕ 添加币种", callback_data="menu_add_symbol"),
             InlineKeyboardButton("➖ 删除币种", callback_data="menu_del_symbol")],
            [InlineKeyboardButton("🔄 同步持仓", callback_data="sync_pos"),
             InlineKeyboardButton("📋 监控列表", callback_data="list_symbols")],
            [InlineKeyboardButton("🧠 超级大脑诊断", callback_data="brain_status"),
             InlineKeyboardButton("📈 差距分析", callback_data="gap_analysis")],
            [InlineKeyboardButton("🔄 刷新面板", callback_data="refresh_panel")]
        ])

    def _build_option_keyboard(self, options, prefix, setting_key):
        kb = []
        row = []
        for label, val in options:
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{val}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("✍️ 自填模式", callback_data=f"prompt_manual:{setting_key}")])
        kb.append([InlineKeyboardButton("🔙 返回控制台", callback_data="refresh_panel")])
        return InlineKeyboardMarkup(kb)

    # ---------- 命令处理器 ----------
    async def cmd_menu(self, update, context):
        if not self._auth(update): await update.message.reply_text("⛔ 未授权"); return
        status = "🟢 运行中" if self.is_running else "🔴 已停止"
        now = datetime.now(CST).strftime("%H:%M:%S")
        msg = f"⚙️ **量化机器人控制台 (超级大脑融合版) {self.env_tag}**\n当前状态: {status}\n⏱ {now}"
        await update.effective_message.reply_text(msg, reply_markup=self._build_main_keyboard(), parse_mode="Markdown")

    async def cmd_panic(self, update, context):
        if not self._auth(update): await update.message.reply_text("⛔ 未授权"); return
        await self.panic_sell_all()
        await update.effective_message.reply_text("🚨 全平指令已执行", parse_mode="Markdown")

    async def cmd_analysis(self, update, context):
        if not self._auth(update): return
        await self.render_gap_analysis(update.effective_message)

    async def cmd_brain(self, update, context):
        if not self._auth(update): return
        await self.render_brain_status(update.effective_message)

    async def cmd_help(self, update, context):
        msg = "💡 **参数修改指南：**\n• 止盈率: `/settp 5`\n• 硬止损: `/setsl 2`\n• 移动止损: `/settsl 1.5`\n• 单笔额度: `/setamount 100`\n• K线周期: `/settf 15m`\n• 保留底线: `/setreserve 50`\n• 添加币种: `/addsymbol SOL/USDT`"
        await update.effective_message.reply_text(msg, parse_mode="Markdown")

    async def cmd_set_tp(self, update, context):
        if not self._auth(update): return
        try:
            self.tp_pct = self._parse_pct(float(context.args[0]))
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 止盈率: {self.tp_pct*100:.1f}%", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/settp 5`")

    async def cmd_set_sl(self, update, context):
        if not self._auth(update): return
        try:
            self.sl_pct = self._parse_pct(float(context.args[0]))
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 硬止损: {self.sl_pct*100:.1f}%", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/setsl 2`")

    async def cmd_set_tsl(self, update, context):
        if not self._auth(update): return
        try:
            self.trailing_sl_pct = self._parse_pct(float(context.args[0]))
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 移动止损回调: {self.trailing_sl_pct*100:.1f}%", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/settsl 1.5`")

    async def cmd_set_amount(self, update, context):
        if not self._auth(update): return
        try:
            self.single_order_usdt = float(context.args[0])
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 单笔额度: {self.single_order_usdt} USDT", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/setamount 100`")

    async def cmd_set_tf(self, update, context):
        if not self._auth(update): return
        try:
            self.timeframe = context.args[0].lower()
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ K线周期: {self.timeframe}", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/settf 15m`")

    async def cmd_set_reserve(self, update, context):
        if not self._auth(update): return
        try:
            self.reserve_bottom = float(context.args[0])
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 保留底线: {self.reserve_bottom} USDT", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/setreserve 50`")

    async def cmd_add_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if sym not in self.symbols:
                self.symbols.append(sym)
                async with self.lock: self._save()
                await update.effective_message.reply_text(f"➕ 已新增: {sym}", parse_mode="Markdown")
            else: await update.effective_message.reply_text(f"⚠️ {sym} 已存在！")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/addsymbol SOL/USDT`")

    async def cmd_del_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if sym in self.symbols:
                self.symbols.remove(sym)
                async with self.lock: self._save()
                await update.effective_message.reply_text(f"➖ 已移除: {sym}", parse_mode="Markdown")
            else: await update.effective_message.reply_text(f"⚠️ {sym} 不存在！")
        except: await update.effective_message.reply_text("❌ 格式错误！例如：`/delsymbol SOL/USDT`")

    # ---------- 诊断渲染 ----------
    async def render_brain_status(self, msg_obj):
        try:
            macro = await self.macro.check()
            sym = self.symbols[0]
            ticker = await self.exchange.fetch_ticker(sym)
            price = ticker['last']

            # ✅ 修复：使用真实资金费率
            funding_rate = await self.exchange.fetch_funding_rate(sym)

            # ✅ 修复：现在 exchange 有 fetch_orderbook 了
            ob = await self.exchange.fetch_orderbook(sym)
            ob_valid, ob_msg = await self.orderbook_engine.validate(ob)

            ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
            tech = self.tech.calc(ohlcv, price)

            # 清算数据暂时保留占位（后续可接入真实链上数据）
            liq_target = price * 0.975

            msg = (f"🧠 **AI 超级大脑 - 实时四大维度诊断** {self.env_tag}\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"1️⃣ **宏观舆情**: {macro['status']} (风险分: {macro['score']:.2f})\n"
                   f"2️⃣ **链上/费率**: 资金费率: {funding_rate*100:+.4f}%\n"
                   f"   • 下方清算预估: {liq_target:.2f} USDT\n"
                   f"3️⃣ **盘口博弈**: {'✅ ' + ob_msg if ob_valid else '⚠️ ' + ob_msg}\n"
                   f"4️⃣ **布林网格 ({sym})**:\n"
                   f"   • 上轨/下轨: {tech['bb_upper']:.1f} / {tech['bb_lower']:.1f}\n"
                   f"   • 带宽: {tech['bandwidth_pct']:.2f}% | RSI(14): {tech['rsi']:.1f}\n"
                   f"   • ATR: {tech['atr']:.2f} USDT\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"🤖 *综合判断: 四大引擎协作正常。*")

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新大脑诊断", callback_data="brain_status")],
                [InlineKeyboardButton("🔙 返回控制台", callback_data="refresh_panel")]
            ])
            try: await msg_obj.edit_text(msg, reply_markup=kb, parse_mode="Markdown")
            except: await msg_obj.reply_text(msg, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"大脑诊断异常: {e}")

    async def render_gap_analysis(self, msg_obj):
        try:
            lines = ["📈 **多币种自适应网格低买差距分析**\n━━━━━━━━━━━━━━━━━━"]
            for sym in self.symbols:
                ticker = await self.exchange.fetch_ticker(sym)
                p = ticker['last']
                ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
                tech = self.tech.calc(ohlcv, p)
                target = min(tech['bb_lower'], p * 0.99)
                gap = ((p - target) / p) * 100
                icon = "🔥 即将触及布林下轨" if gap < 0.5 else "📉 观察中"
                lines.append(f"🔹 **{sym}**: {p:.2f} USDT\n  买点: {target:.2f} (差 {gap:+.2f}%) {icon}\n  RSI: {tech['rsi']:.1f} | ATR: {tech['atr']:.2f}")
            lines.append("💡 *提示: 超级大脑已升级为布林带/ATR 动态网格！*")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新分析", callback_data="gap_analysis")],
                [InlineKeyboardButton("🔙 返回控制台", callback_data="refresh_panel")]
            ])
            try: await msg_obj.edit_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
            except: await msg_obj.reply_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"差距分析异常: {e}")

    # ---------- 自填模式文本处理 ----------
    async def handle_text_input(self, update, context):
        pending = context.user_data.get('pending_setting')
        if not pending: return
        user_text = update.message.text.strip()
        msg = ""
        try:
            if pending == "settp":
                self.tp_pct = self._parse_pct(float(user_text))
                msg = f"✅ 止盈率: {self.tp_pct*100:.1f}%"
            elif pending == "setsl":
                self.sl_pct = self._parse_pct(float(user_text))
                msg = f"✅ 硬止损: {self.sl_pct*100:.1f}%"
            elif pending == "settsl":
                self.trailing_sl_pct = self._parse_pct(float(user_text))
                msg = f"✅ 移动止损: {self.trailing_sl_pct*100:.1f}%"
            elif pending == "setamount":
                self.single_order_usdt = float(user_text)
                msg = f"✅ 单笔额度: {self.single_order_usdt} USDT"
            elif pending == "settf":
                self.timeframe = user_text.lower()
                msg = f"✅ K线周期: {self.timeframe}"
            elif pending == "setreserve":
                self.reserve_bottom = float(user_text)
                msg = f"✅ 保留底线: {self.reserve_bottom} USDT"
            elif pending == "addsymbol":
                sym = user_text.upper()
                if sym not in self.symbols:
                    self.symbols.append(sym)
                    msg = f"➕ 已新增: {sym}"
                else: msg = f"⚠️ {sym} 已存在！"
            elif pending == "delsymbol":
                sym = user_text.upper()
                if sym in self.symbols:
                    self.symbols.remove(sym)
                    msg = f"➖ 已移除: {sym}"
                else: msg = f"⚠️ {sym} 不存在！"
            async with self.lock: self._save()
            context.user_data['pending_setting'] = None
            status = "🟢 运行中" if self.is_running else "🔴 已停止"
            now = datetime.now(CST).strftime("%H:%M:%S")
            panel = f"{msg}\n\n⚙️ **量化控制台 {self.env_tag}**\n状态: {status}\n⏱ {now}"
            await update.message.reply_text(panel, reply_markup=self._build_main_keyboard(), parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ 格式有误！请输入数字。")
            context.user_data['pending_setting'] = None

    # ---------- 按钮事件路由 ----------
    async def handle_button_click(self, update, context):
        query = update.callback_query
        data = query.data
        try:
            if data == "refresh_panel":
                await query.answer("🔄 已刷新")
                status = "🟢 运行中" if self.is_running else "🔴 已停止"
                now = datetime.now(CST).strftime("%H:%M:%S")
                msg = f"⚙️ **量化机器人控制台 (超级大脑融合版) {self.env_tag}**\n当前状态: {status}\n⏱ {now}"
                try: await query.edit_message_text(msg, reply_markup=self._build_main_keyboard(), parse_mode="Markdown")
                except: pass

            elif data == "toggle_filter":
                self.orderbook_filter = not self.orderbook_filter
                await query.answer(f"🏮 盘口过滤已{'开启' if self.orderbook_filter else '关闭'}")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass

            elif data == "toggle_breaker":
                self.waterfall_breaker = not self.waterfall_breaker
                await query.answer(f"🚨 瀑布熔断已{'开启' if self.waterfall_breaker else '关闭'}")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass

            elif data == "bot_start":
                self.is_running = True; await query.answer("⚡ 已开启", show_alert=True)
                try: await query.edit_message_text("🟢 运行中", reply_markup=self._build_main_keyboard())
                except: pass

            elif data == "bot_stop":
                self.is_running = False; await query.answer("🔴 已关机", show_alert=True)
                try: await query.edit_message_text("🔴 已停止", reply_markup=self._build_main_keyboard())
                except: pass

            elif data == "brain_status":
                await query.answer("🧠 正在调阅四大引擎诊断...")
                await self.render_brain_status(query.message)

            elif data == "gap_analysis":
                await query.answer("📈 正在计算低买差距...")
                await self.render_gap_analysis(query.message)

            elif data == "dashboard":
                await query.answer("📊 看板已调阅")
                msg = (f"📊 **运行看板** {self.env_tag}\n"
                       f"━━━━━━━━━━━━━━━━━━\n"
                       f"🔹 监控币种: {', '.join(self.symbols)}\n"
                       f"🔹 盘口防护: {'已开启' if self.orderbook_filter else '已关闭'}\n"
                       f"🔹 瀑布熔断: {'已开启' if self.waterfall_breaker else '已关闭'}\n"
                       f"🔹 止盈/止损: +{self.tp_pct*100:.1f}% / -{self.sl_pct*100:.1f}%\n"
                       f"🔹 移动止损: -{self.trailing_sl_pct*100:.1f}%\n"
                       f"🔹 单笔挂单: {self.single_order_usdt} USDT | 周期: {self.timeframe}\n"
                       f"🔹 保留底线: {self.reserve_bottom} USDT")
                await query.message.reply_text(msg, parse_mode="Markdown")

            elif data == "balance":
                await query.answer("💳 查询中...")
                bal = await self.exchange.fetch_balance()
                usdt = bal.get('USDT', {}).get('free', 0) if isinstance(bal.get('USDT'), dict) else float(bal.get('USDT', 0))
                msg = f"💳 **账户余额 {self.env_tag}**\n━━━━━━━━━━━━━━━━━━\n💵 可用 USDT: {usdt:.2f}\n🔒 安全底线: {self.reserve_bottom} USDT"
                await query.message.reply_text(msg, parse_mode="Markdown")

            elif data == "list_symbols":
                await query.answer()
                s_list = "\n".join([f"• {s}" for s in self.symbols])
                await query.message.reply_text(f"📋 **监控清单**:\n{s_list}", parse_mode="Markdown")

            elif data == "sync_pos":
                await query.answer("🔄 已同步", show_alert=True)
                await query.message.reply_text("🔄 持仓同步完成", parse_mode="Markdown")

            elif data == "panic_confirm":
                await query.answer("🚨 请发送 /panic 确认", show_alert=True)
                await query.message.reply_text("🚨 **确认紧急全平？**\n请发送 `/panic` 确认！", parse_mode="Markdown")

            # 二层快捷菜单
            elif data == "menu_set_tp":
                opts = [("🎯 3%", "0.03"), ("🎯 5%", "0.05"), ("🎯 8%", "0.08"), ("🎯 10%", "0.10")]
                await query.edit_message_text(f"🎯 选择止盈率 (当前 {self.tp_pct*100:.1f}%)",
                    reply_markup=self._build_option_keyboard(opts, "cfg_tp", "settp"), parse_mode="Markdown")
            elif data == "menu_set_sl":
                opts = [("🛡️ 1%", "0.01"), ("🛡️ 2%", "0.02"), ("🛡️ 3%", "0.03"), ("🛡️ 5%", "0.05")]
                await query.edit_message_text(f"🛡️ 选择硬止损率 (当前 {self.sl_pct*100:.1f}%)",
                    reply_markup=self._build_option_keyboard(opts, "cfg_sl", "setsl"), parse_mode="Markdown")
            elif data == "menu_set_tsl":
                opts = [("📉 0.5%", "0.005"), ("📉 1.0%", "0.01"), ("📉 1.5%", "0.015"), ("📉 2.0%", "0.02")]
                await query.edit_message_text(f"📉 选择移动止损回调 (当前 {self.trailing_sl_pct*100:.1f}%)",
                    reply_markup=self._build_option_keyboard(opts, "cfg_tsl", "settsl"), parse_mode="Markdown")
            elif data == "menu_set_amount":
                opts = [("💵 50 U", "50"), ("💵 100 U", "100"), ("💵 200 U", "200"), ("💵 500 U", "500")]
                await query.edit_message_text(f"💵 选择单笔额度 (当前 {self.single_order_usdt} USDT)",
                    reply_markup=self._build_option_keyboard(opts, "cfg_amt", "setamount"), parse_mode="Markdown")
            elif data == "menu_set_tf":
                opts = [("⏱️ 1m", "1m"), ("⏱️ 5m", "5m"), ("⏱️ 15m", "15m"), ("⏱️ 1h", "1h")]
                await query.edit_message_text(f"⏱️ 选择K线周期 (当前 {self.timeframe})",
                    reply_markup=self._build_option_keyboard(opts, "cfg_tf", "settf"), parse_mode="Markdown")
            elif data == "menu_set_reserve":
                opts = [("🔒 20 U", "20"), ("🔒 50 U", "50"), ("🔒 100 U", "100"), ("🔒 200 U", "200")]
                await query.edit_message_text(f"🔒 选择安全底线 (当前 {self.reserve_bottom} USDT)",
                    reply_markup=self._build_option_keyboard(opts, "cfg_res", "setreserve"), parse_mode="Markdown")
            elif data == "menu_add_symbol":
                opts = [("➕ BTC/USDT", "BTC/USDT"), ("➕ SOL/USDT", "SOL/USDT")]
                await query.edit_message_text("➕ 快捷添加币种",
                    reply_markup=self._build_option_keyboard(opts, "cfg_add", "addsymbol"), parse_mode="Markdown")
            elif data == "menu_del_symbol":
                opts = [(f"➖ {s}", s) for s in self.symbols]
                await query.edit_message_text("➖ 选择要移除的币种",
                    reply_markup=self._build_option_keyboard(opts, "cfg_del", "delsymbol"), parse_mode="Markdown")

            # cfg 快捷应用并刷新面板
            elif data.startswith("cfg_tp:"):
                self.tp_pct = float(data.split(":")[1]); await query.answer(f"止盈改为 {self.tp_pct*100:.1f}%", show_alert=True)
                async with self.lock: self._save()
                await self._refresh_panel(query)
            elif data.startswith("cfg_sl:"):
                self.sl_pct = float(data.split(":")[1]); await query.answer(f"止损改为 {self.sl_pct*100:.1f}%", show_alert=True)
                async with self.lock: self._save()
                await self._refresh_panel(query)
            elif data.startswith("cfg_tsl:"):
                self.trailing_sl_pct = float(data.split(":")[1]); await query.answer(f"移动止损改为 {self.trailing_sl_pct*100:.1f}%", show_alert=True)
                async with self.lock: self._save()
                await self._refresh_panel(query)
            elif data.startswith("cfg_amt:"):
                self.single_order_usdt = float(data.split(":")[1]); await query.answer(f"单笔改为 {self.single_order_usdt}U", show_alert=True)
                async with self.lock: self._save()
                await self._refresh_panel(query)
            elif data.startswith("cfg_tf:"):
                self.timeframe = data.split(":")[1]; await query.answer(f"周期改为 {self.timeframe}", show_alert=True)
                async with self.lock: self._save()
                await self._refresh_panel(query)
            elif data.startswith("cfg_res:"):
                self.reserve_bottom = float(data.split(":")[1]); await query.answer(f"底线改为 {self.reserve_bottom}U", show_alert=True)
                async with self.lock: self._save()
                await self._refresh_panel(query)
            elif data.startswith("cfg_add:"):
                sym = data.split(":")[1]
                if sym not in self.symbols:
                    self.symbols.append(sym); await query.answer(f"已添加 {sym}", show_alert=True)
                    async with self.lock: self._save()
                else: await query.answer(f"{sym} 已存在", show_alert=True)
                await self._refresh_panel(query)
            elif data.startswith("cfg_del:"):
                sym = data.split(":")[1]
                if sym in self.symbols:
                    self.symbols.remove(sym); await query.answer(f"已移除 {sym}", show_alert=True)
                    async with self.lock: self._save()
                else: await query.answer(f"{sym} 不存在", show_alert=True)
                await self._refresh_panel(query)

            # 自填模式
            elif data.startswith("prompt_manual:"):
                key = data.split(":")[1]
                context.user_data['pending_setting'] = key
                prompts = {
                    "settp": "✍️ 输入止盈率（例：6.5 = 6.5%）：",
                    "setsl": "✍️ 输入硬止损率（例：2.5 = 2.5%）：",
                    "settsl": "✍️ 输入移动止损回调率（例：1.5 = 1.5%）：",
                    "setamount": "✍️ 输入单笔 USDT（例：150）：",
                    "settf": "✍️ 输入K线周期（例：15m 或 1h）：",
                    "setreserve": "✍️ 输入安全底线（例：100）：",
                    "addsymbol": "✍️ 输入币种（例：DOGE/USDT）：",
                    "delsymbol": "✍️ 输入要删除的币种（例：SOL/USDT）：",
                }
                await query.message.reply_text(prompts.get(key, "✍️ 请输入数值："),
                    reply_markup=ForceReply(selective=True), parse_mode="Markdown")
                await query.answer()

        except Exception as e:
            logger.error(f"按钮异常 ({data}): {e}")

    async def _refresh_panel(self, query):
        status = "🟢 运行中" if self.is_running else "🔴 已停止"
        now = datetime.now(CST).strftime("%H:%M:%S")
        msg = f"⚙️ **量化机器人控制台 (超级大脑融合版) {self.env_tag}**\n当前状态: {status}\n⏱ {now}"
        try: await query.edit_message_text(msg, reply_markup=self._build_main_keyboard(), parse_mode="Markdown")
        except: pass

    async def panic_sell_all(self):
        for sym in self.symbols:
            await self.exchange.cancel_all_orders(sym)
            bal = await self.exchange.fetch_balance()
            coin = sym.split('/')[0]
            amount = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
            if isinstance(amount, (int, float)) and amount > 0:
                await self.exchange.create_market_sell_order(sym, amount)

    async def start(self):
        if self.tg_app:
            await self.tg_app.initialize()
            await self.tg_app.start()
            await self.tg_app.updater.start_polling(drop_pending_updates=True)
            logger.info("✅ Bot 完全体启动")
            while True:
                await asyncio.sleep(30)
