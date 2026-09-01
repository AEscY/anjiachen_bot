"""
signals.py - 信号层

职责：判断「此刻是否适合为某币种建立/维持网格」。

网格是震荡市策略，在单边下跌中必然亏损（回测下跌行情 -7.6%~-10.6%）。
因此网格模式下的信号层不做"选币打分"，而是做**择时过滤**：
趋势良好 → 允许开网；趋势恶化 → 停止新开网（已挂单继续吃网格利润）。

单次低吸高卖模式仍使用原来的 5 因子评分。
"""
import logging

logger = logging.getLogger(__name__)


class SignalEngine:
    """网格择时：只在趋势不过度恶化时允许开新网"""

    @staticmethod
    def grid_entry_allowed(tech: dict, cfg) -> tuple:
        """
        返回 (bool, 说明)
        tech: TechnicalEngine.calc() 的输出
        """
        if not tech:
            return False, "指标缺失"

        if not bool(cfg.grid_trend_filter):
            return True, "未启用趋势过滤"

        trend = float(tech.get("trend_strength", 0) or 0)
        thr = float(cfg.grid_trend_threshold)

        # 趋势强度为 EMA 斜率，负值代表下跌
        if trend <= -thr:
            return False, f"下跌趋势({trend*100:.1f}%)"
        return True, f"趋势{trend*100:+.1f}%"

    @staticmethod
    def atr_pct(tech: dict) -> float:
        """从指标中提取 ATR 占价格百分比，供网格间距使用"""
        if not tech:
            return 0.0
        atr = float(tech.get("atr", 0) or 0)
        mid = float(tech.get("bb_middle", 0) or 0)
        if mid > 0 and atr > 0:
            return atr / mid
        return 0.0


class ScoreEngine:
    """
    单次低吸高卖模式的 5+1 因子评分（从 bot.py 抽出）。

    修复记录：恐惧贪婪因子原本被硬编码为常数 50，
    导致每次真实请求 API 后数据被直接丢弃。现已接入。
    """

    @staticmethod
    def score(sym, price, tech, fg, orderbook, ticker, cfg) -> dict:
        if tech is None:
            return {"should_open": False, "score": 50.0, "details": ["指标缺失"],
                    "amount": 0.0}

        # 1) RSI
        rsi = float(tech.get("rsi", 50) or 50)
        rsi_score = max(0.0, min(100.0, 50 + (50 - rsi) * 0.8))

        # 2) 布林带位置
        bb_lower = float(tech.get("bb_lower", 0) or 0)
        bb_upper = float(tech.get("bb_upper", 0) or 0)
        if bb_upper > bb_lower and price > 0:
            bb_pos = (price - bb_lower) / (bb_upper - bb_lower)
            bb_score = 100 - bb_pos * 100
        else:
            bb_score = 50.0
        bb_score = max(0.0, min(100.0, bb_score))

        # 3) OFI 盘口失衡
        ofi_score = 50.0
        if orderbook:
            bids = orderbook.get("bids", []) or []
            asks = orderbook.get("asks", []) or []
            if len(bids) >= 5 and len(asks) >= 5:
                bv = sum(float(b[1]) for b in bids[:5])
                av = sum(float(a[1]) for a in asks[:5])
                ofi = (bv - av) / (bv + av + 1e-6)
                ofi_score = 50 + ofi * 40

        # 4) 趋势惩罚
        trend = float(tech.get("trend_strength", 0) or 0)
        thr = float(getattr(cfg, "TREND_THRESHOLD", 0.02))
        if trend > thr:
            trend_penalty = 30.0
        elif trend < -thr:
            trend_penalty = 15.0
        else:
            trend_penalty = 0.0

        # 5) 成交量
        vol = float((ticker or {}).get("volume", 0) or 0)
        vol_score = 50 + min(20.0, (vol / 1000) * 0.5)

        # 6) 恐惧贪婪（此前硬编码为 50）
        if fg is None:
            fg_score = 50.0
        else:
            fg_score = max(0.0, min(100.0, 100.0 - float(fg)))

        total = (rsi_score * 0.25 + bb_score * 0.25 + ofi_score * 0.20 +
                 vol_score * 0.15 + fg_score * 0.15) - trend_penalty
        total = max(0.0, min(100.0, total))

        offset = int(getattr(cfg, "_adaptive_score_offset", 0) or 0)
        threshold = max(40, min(95, int(cfg.auto_min_score) + offset))
        should = total >= threshold and trend_penalty < 30

        amount = float(cfg.single_order_usdt) * float(
            getattr(cfg, "_adaptive_amount_factor", 1.0) or 1.0)

        return {
            "should_open": should,
            "score": total,
            "threshold": threshold,
            "details": [f"RSI:{rsi:.0f}", f"BB:{bb_score:.0f}", f"OFI:{ofi_score:.0f}",
                        f"FG:{fg_score:.0f}", f"趋势:{trend*100:.1f}%",
                        f"惩罚:{trend_penalty:.0f}"],
            "amount": amount,
        }
