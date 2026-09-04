"""
execution.py - 执行层

职责：
  1. reconcile()：把网格的「目标订单」同步到交易所。
     对比目标与交易所未成交单 → 缺的补挂、多的撤销。
     这是「声明式」执行：不记录"我下过什么单"，而是每次重新计算应该有什么单，
     因此重启后自动收敛到正确状态，不存在状态漂移。

  2. 幂等：每个网格档位的订单带固定 client_id。
     交易所侧同一 client_id 重复提交不会产生第二张单，
     即使 reconcile 在网络抖动后重跑也不会重复下单。

  3. 成交检测：轮询未成交单，消失的即为成交/撤销，
     再查单确认最终状态并回调网格引擎。
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, exchange, grid, cfg):
        self.exchange = exchange
        self.grid = grid
        self.cfg = cfg
        # client_id -> 交易所 order_id（挂单成功后记录）
        self.live: dict = {}
        # 上次 reconcile 时间，避免频繁刷接口
        self._last_sync: dict = {}
        self._sync_interval = 20

    # ─────────── 主入口 ───────────

    async def sync_symbol(self, symbol, price, atr_pct, equity_usdt,
                          force=False, budget_is_net=False) -> dict:
        """
        同步单个币种的网格订单。
        返回 {'placed': n, 'cancelled': n, 'filled': [...]}
        """
        now = time.time()
        if not force and now - self._last_sync.get(symbol, 0) < self._sync_interval:
            return {"placed": 0, "cancelled": 0, "filled": []}
        self._last_sync[symbol] = now
        self._cur_symbol = symbol

        result = {"placed": 0, "cancelled": 0, "filled": []}

        # 1) 检测已有挂单的成交情况
        filled = await self._check_fills(symbol)
        result["filled"] = filled

        # 2) 计算目标订单
        desired = self.grid.desired_orders(symbol, price, atr_pct,
                                           equity_usdt,
                                           budget_is_net=budget_is_net)

        # 3) 拉取交易所当前未成交单
        try:
            open_orders = await self.exchange.fetch_open_orders(symbol) or []
        except Exception as e:
            logger.warning(f"查询未成交单失败 {symbol}: {e}")
            return result

        # 以 client_id 建立索引
        open_by_cid = {}
        for o in open_orders:
            cid = (o.get("clientOrderId") or o.get("client_order_id")
                   or (o.get("info") or {}).get("clOrdId")
                   or (o.get("info") or {}).get("clientOrderId") or "")
            if cid:
                open_by_cid[str(cid)] = o

        # 4) 撤销目标中不再需要的单
        want_cids = {o.client_id for o in desired}
        for cid, o in list(open_by_cid.items()):
            if cid not in want_cids:
                oid = o.get("id")
                if oid and await self.exchange.cancel_order(oid, symbol):
                    self.live.pop(cid, None)
                    result["cancelled"] += 1
                    logger.info(f"🗑️  撤销多余挂单 {symbol} {oid}")
                    self._clear_watch_order(symbol, cid)

        # 5) 补挂缺失的单
        for d in desired:
            if d.client_id in open_by_cid:
                self._track(d.client_id, open_by_cid[d.client_id].get("id"),
                            d.side, d.level)
                continue
            oid = await self._place(symbol, d)
            if oid:
                self._track(d.client_id, oid, d.side, d.level)
                result["placed"] += 1

        return result

    def _track(self, cid, oid, side, level):
        """记录挂单的方向与档位，供成交回调使用"""
        self.live[str(cid)] = {"id": oid, "side": side, "level": int(level)}
        # 看门狗：记录挂出时间，用于检测"长期不成交"
        try:
            wd = getattr(self.cfg, "watchdog", None)
            sym = getattr(self, "_cur_symbol", None)
            if wd is not None and sym:
                wd.record_order(sym, cid)
        except Exception:
            pass

    def _clear_watch_order(self, symbol, cid):
        """通知看门狗：这张单已成交/撤销，不再计入"长期未成交" """
        try:
            wd = getattr(self.cfg, "watchdog", None)
            if wd is not None:
                wd.clear_order(symbol, cid)
        except Exception:
            pass

    # ─────────── 挂单 ───────────

    async def _place(self, symbol, order) -> str:
        """挂出一张限价单（order_type=market 时退回市价）"""
        try:
            price = await self.exchange.round_price(symbol, order.price)
            qty = await self.exchange.round_amount(symbol, order.qty)
            if price <= 0 or qty <= 0:
                # 此前静默返回导致档位卡死且无任何线索，改为显式告警
                logger.warning(
                    f"⚠️ 跳过非法挂单 {symbol} {order.side} L{order.level}: "
                    f"price={order.price} qty={order.qty} → 精度后 price={price} qty={qty}")
                return ""

            if str(self.cfg.order_type).lower() == "market":
                # 市价模式：立即成交，直接回调网格
                if order.side == "buy":
                    res = await self.exchange.create_market_buy_order(symbol, qty)
                else:
                    res = await self.exchange.create_market_sell_order(symbol, qty)
                if res:
                    self._notify_fill(symbol, order, res)
                return res.get("id", "") if res else ""

            res = await self.exchange.create_limit_order(
                symbol, order.side, qty, price, client_id=order.client_id)
            if res:
                logger.debug(f"📤 挂单 {symbol} {order.side} {qty:.6f} @ {price:.4f}")
                return res.get("id", "")
            return ""
        except Exception as e:
            logger.error(f"挂单失败 {symbol} {order.side}: {e}")
            return ""

    # ─────────── 成交检测 ───────────

    async def _check_fills(self, symbol) -> list:
        """
        检查 self.live 中的单是否已成交。
        单从「未成交列表」消失 且 fetch_order 显示 closed → 成交。
        """
        out = []
        if not self.live:
            return out

        try:
            open_orders = await self.exchange.fetch_open_orders(symbol) or []
        except Exception as e:
            logger.debug(f"查询未成交单失败: {e}")
            return out

        open_ids = {str(o.get("id")) for o in open_orders}

        for cid, rec in list(self.live.items()):
            oid = rec.get("id") if isinstance(rec, dict) else rec
            if not oid or str(oid) in open_ids:
                continue      # 还挂着，未成交
            # 已从未成交列表消失 → 查最终状态
            try:
                o = await self.exchange.fetch_order(oid, symbol)
            except Exception as e:
                logger.debug(f"查单失败 {oid}: {e}")
                continue

            if not o:
                self.live.pop(cid, None)
                self._clear_watch_order(symbol, cid)
                continue

            status = str(o.get("status") or "").lower()
            filled = float(o.get("filled") or 0)
            if status == "closed" and filled > 0:
                await self._handle_fill(symbol, cid, o)
                out.append({"client_id": cid, "order": o})
                self.live.pop(cid, None)
                self._clear_watch_order(symbol, cid)
            elif status in ("canceled", "cancelled", "expired", "rejected"):
                self.live.pop(cid, None)
                self._clear_watch_order(symbol, cid)

        return out

    async def _handle_fill(self, symbol, cid, order):
        """
        把成交结果回调给网格引擎。
        买卖方向与档位取自挂单时记录的值 —— 不可从 client_id 反推，
        因为币种名本身可能含 'S'（如 USDT），会导致买单被误判为卖单。
        """
        rec = self.live.get(cid) or {}
        is_sell = rec.get("side") == "sell"
        level = int(rec.get("level", 0) or 0)
        price = float(order.get("average") or order.get("price") or 0)
        filled = float(order.get("filled") or 0)
        fee = float((order.get("fee") or {}).get("cost") or 0)
        fee_cur = (order.get("fee") or {}).get("currency") or ""

        if is_sell:
            self.grid.on_sell_filled(symbol, level, filled, price, fee, fee_cur)
        else:
            self.grid.on_buy_filled(symbol, level, filled, price, fee, fee_cur,
                                    order.get("id", ""))

    def _notify_fill(self, symbol, gorder, res):
        """市价模式下的即时回调"""
        price = float(res.get("average") or 0)
        filled = float(res.get("filled") or 0)
        fee = float(res.get("_fee_cost") or 0)
        fee_cur = res.get("_fee_currency", "")
        if gorder.side == "sell":
            self.grid.on_sell_filled(symbol, gorder.level, filled, price, fee, fee_cur)
        else:
            self.grid.on_buy_filled(symbol, gorder.level, filled, price, fee,
                                    fee_cur, res.get("id", ""))

    @staticmethod
    def _level_from_cid(cid: str) -> int:
        """从 client_id 末尾解析档位号"""
        tail = cid[-2:]
        digits = "".join(ch for ch in tail if ch.isdigit())
        return int(digits) if digits else 0

    # ─────────── 清仓 ───────────

    async def liquidate(self, symbol, price) -> bool:
        """网格止损：撤销所有挂单并市价清仓"""
        try:
            await self.exchange.cancel_all_orders(symbol)
        except Exception as e:
            logger.warning(f"撤销挂单失败 {symbol}: {e}")
        self.live.clear()

        st = self.grid.get_state(symbol)
        if not st or not st.lots:
            return True

        total_qty = sum(float(l["qty"]) for l in st.lots.values())
        if total_qty <= 0:
            return True

        qty = await self.exchange.round_amount(symbol, total_qty)
        if qty <= 0:
            return False
        res = await self.exchange.create_market_sell_order(symbol, qty)
        if not res:
            return False

        filled = float(res.get("filled") or 0)
        avg = float(res.get("average") or price)
        fee = float(res.get("_fee_cost") or 0)
        fee_cur = res.get("_fee_currency", "")

        # 逐档核销
        for lvl in list(st.lots.keys()):
            lot = st.lots.get(lvl)
            if not lot:
                continue
            q = min(float(lot["qty"]), filled)
            if q <= 0:
                break
            self.grid.on_sell_filled(symbol, int(lvl), q, avg,
                                     fee * (q / max(total_qty, 1e-12)), fee_cur)
            filled -= q

        # ⚠️ 清仓残留必须核销，不能留在账本里。
        #
        # 成因：round_amount 按交易所精度取整后，实际卖出量可能
        # 略小于持仓总量（例如 0.50000 → 0.49999）。
        # 逐档核销时最后一档只能分到 0.09999，留下 0.00001 的"灰尘"。
        #
        # 实测：5 档持仓市价清仓后，账本残留 1 档微小数量，
        # 导致"清仓后无持仓"断言失败。
        #
        # 后果：
        #   · 账本与交易所永久差一笔灰尘 → 对账不一致
        #   · 网格状态不干净 → 下次建网读到幽灵持仓
        #   · 残留档位继续挂卖单，但数量低于交易所最小额 → 反复失败
        #
        # 处理：清仓语义就是"全部卖出"，残留部分按成交价核销，
        # 对应成本计入已实现亏损，然后清空档位。
        st_now = self.grid.get_state(symbol)
        if st_now and st_now.lots:
            residual_qty = sum(float(l.get("qty") or 0)
                               for l in st_now.lots.values())
            residual_cost = sum(float(l.get("cost_usdt") or 0)
                                for l in st_now.lots.values())
            if residual_qty > 0:
                dust_pnl = residual_qty * avg - residual_cost
                st_now.realized_pnl += dust_pnl
                logger.warning(
                    f"🧹 {symbol} 清仓残留 {residual_qty:.8f} "
                    f"（精度截断灰尘，成本 {residual_cost:.4f} U）已核销，"
                    f"计入盈亏 {dust_pnl:+.4f} U")
            st_now.lots.clear()
            st_now.pending_client_ids.clear()
        return True
