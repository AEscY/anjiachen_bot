# app.py — OKX 策略管家 · 小白入门版（DRY_RUN 优先）
# 功能：Telegram 菜单遥控 + 低吸高卖引擎 + 风控熔断 + Render 保活端点
# 跑通后可替换为完整版（含网格模式 UI / 风控中心 / 交易记录）

import os, time, threading, requests
from flask import Flask, jsonify

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
DRY_RUN  = os.environ.get("DRY_RUN", "1") == "1"
TG = f"https://api.telegram.org/bot{TG_TOKEN}"

# ---------- 低吸高卖参数（UI 可改，引擎直接读） ----------
CFG = {
    "instId": "BTC-USDT",
    "base": None,          # 启动时自动取当前价
    "dip_pct": 0.02,       # 跌 2% 买入
    "rally_pct": 0.03,     # 涨 3% 卖出
    "sz": 0.0001,          # 单笔数量(BTC)
    "max_pos": 0.001,      # 最大持仓(BTC)
    "cooldown": 300,       # 冷却秒数
    "running": False,      # 策略开关
    "pos": 0.0,            # 本地持仓记录
    "last_ts": 0,
}
RISK = {"max_order": 200, "breaker": False, "reason": ""}

# ---------- OKX 公开行情（不需要密钥） ----------
def get_price():
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker?instId=" + CFG["instId"],
            timeout=10).json()
        return float(r["data"][0]["last"])
    except Exception:
        return None

# ---------- Telegram 封装 ----------
def tg_send(text, kb=None, edit_mid=None):
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = {"inline_keyboard": kb}
    try:
        if edit_mid:
            payload["message_id"] = edit_mid
            requests.post(f"{TG}/editMessageText", json=payload, timeout=15)
        else:
            requests.post(f"{TG}/sendMessage", json=payload, timeout=15)
    except Exception:
        pass

def btn(t, d): return {"text": t, "callback_data": d}

# ---------- 页面渲染 ----------
def page_home():
    p = get_price()
    t = (f"🤖 <b>OKX 策略管家</b>  🟢在线\n\n"
         f"📊 {CFG['instId']}: {p:,.0f}\n"
         f"💰 持仓: {CFG['pos']:.4f} BTC\n"
         f"📉 低吸高卖: {'▶️运行中' if CFG['running'] else '⏸️暂停'}\n"
         f"🛡️ 风控: {'⚠️熔断:'+RISK['reason'] if RISK['breaker'] else '✅正常'}\n"
         f"🧪 模式: {'DRY_RUN(只通知不下单)' if DRY_RUN else '实盘'}")
    kb = [[btn("📉 低吸高卖", "dip")],
          [btn("🛡️ 风控", "risk")],
          [btn("🔄 刷新", "home")]]
    return t, kb

def page_dip():
    p = get_price() or 0
    base = CFG["base"] or p
    buy_at = base * (1 - CFG["dip_pct"])
    sell_at = base * (1 + CFG["rally_pct"])
    t = (f"📉 <b>低吸高卖模式</b>\n\n"
         f"状态: {'▶️运行' if CFG['running'] else '⏸️暂停'}\n"
         f"当前价: {p:,.0f} | 基准价: {base:,.0f}\n"
         f"触发: 买入@{buy_at:,.0f} / 卖出@{sell_at:,.0f}\n"
         f"持仓: {CFG['pos']:.4f}/{CFG['max_pos']:.4f} BTC")
    kb = [
        [btn("⏸️ 暂停" if CFG["running"] else "▶️ 启动", "dtog"),
         btn("🔄 重置基准价", "dbase")],
        [btn("✏️ 参数设置", "dparam")],
        [btn("⬅️ 返回主页", "home")],
    ]
    return t, kb

def page_dparam():
    t = ("⚙️ <b>低吸高卖 · 参数</b>\n\n"
         f"跌幅阈值 -{CFG['dip_pct']*100:.1f}%\n"
         f"涨幅阈值 +{CFG['rally_pct']*100:.1f}%\n"
         f"单笔数量 {CFG['sz']} BTC\n"
         f"最大持仓 {CFG['max_pos']} BTC\n"
         f"冷却时间 {CFG['cooldown']} 秒\n\n"
         "点按钮调整，或直接发送数字修改：\n"
         "格式 <code>2.5,3.5,0.0002,0.002,600</code>\n"
         "(跌幅%, 涨幅%, 单笔, 最大持仓, 冷却秒)")
    kb = [
        [btn("−0.5%", "dadj:dip_pct:-0.005"), btn("+0.5%", "dadj:dip_pct:0.005")],
        [btn("−0.5%", "dadj:rally_pct:-0.005"), btn("+0.5%", "dadj:rally_pct:0.005")],
        [btn("−60s", "dadj:cooldown:-60"), btn("+60s", "dadj:cooldown:60")],
        [btn("⬅️ 返回模式页", "dip")],
    ]
    return t, kb

def page_risk():
    t = (f"🛡️ <b>风控</b>\n\n"
         f"单笔金额上限: {RISK['max_order']} USDT\n"
         f"熔断状态: {'⚠️' + RISK['reason'] if RISK['breaker'] else '✅正常'}")
    kb = [[btn("−50", "radj:-50"), btn("+50", "radj:50")]]
    if RISK["breaker"]:
        kb.append([btn("🔄 解除熔断", "rreset")])
    kb.append([btn("⬅️ 返回主页", "home")])
    return t, kb

PAGES = {"home": page_home, "dip": page_dip,
         "dparam": page_dparam, "risk": page_risk}

# ---------- 回调路由 ----------
def on_callback(data, mid):
    if data in PAGES:
        t, kb = PAGES[data]()
        tg_send(t, kb, edit_mid=mid)
    elif data == "dtog":
        CFG["running"] = not CFG["running"]
        if CFG["running"] and CFG["base"] is None:
            p = get_price()
            if p: CFG["base"] = p
        t, kb = page_dip(); tg_send(t, kb, edit_mid=mid)
    elif data == "dbase":
        p = get_price()
        if p:
            CFG["base"] = p
            t, kb = page_dip(); tg_send(t, kb, edit_mid=mid)
    elif data.startswith("dadj:"):
        _, k, d = data.split(":"); d = float(d)
        CFG[k] = round(CFG[k] + d, 4)
        t, kb = page_dparam(); tg_send(t, kb, edit_mid=mid)
    elif data.startswith("radj:"):
        RISK["max_order"] = max(10, RISK["max_order"] + int(data[5:]))
        t, kb = page_risk(); tg_send(t, kb, edit_mid=mid)
    elif data == "rreset":
        RISK["breaker"] = False; RISK["reason"] = ""
        t, kb = page_risk(); tg_send(t, kb, edit_mid=mid)

# ---------- 文本输入 ----------
def on_text(text):
    if text in ("/menu", "/start"):
        t, kb = page_home(); tg_send(t, kb); return
    # 五元组参数格式: 跌%, 涨%, 单笔, 最大持仓, 冷却
    try:
        parts = [x.strip() for x in text.split(",")]
        if len(parts) == 5:
            CFG["dip_pct"]   = max(0.005, min(0.5, float(parts[0]) / 100))
            CFG["rally_pct"] = max(0.005, min(0.5, float(parts[1]) / 100))
            CFG["sz"]        = max(0.00001, float(parts[2]))
            CFG["max_pos"]   = max(CFG["sz"], float(parts[3]))
            CFG["cooldown"]  = max(60, int(float(parts[4])))
            t, kb = page_dparam(); tg_send("✅ 参数已保存\n" + t, kb)
        else:
            tg_send("💡 发送 /menu 打开菜单")
    except Exception:
        tg_send("❌ 格式错误。示例: <code>2.5,3.5,0.0002,0.002,600</code>")

# ---------- 策略引擎（每 10 秒一轮） ----------
def strategy_loop():
    while True:
        try:
            if CFG["running"] and not RISK["breaker"]:
                p = get_price()
                if p:
                    if CFG["base"] is None:
                        CFG["base"] = p
                    dev = (p - CFG["base"]) / CFG["base"]
                    now = time.time()
                    ok_cd = now - CFG["last_ts"] > CFG["cooldown"]
                    usdt = CFG["sz"] * p
                    # 买入：跌幅达标 + 有仓位空间 + 不超单笔风控
                    if (dev <= -CFG["dip_pct"] and ok_cd
                            and CFG["pos"] + CFG["sz"] <= CFG["max_pos"]
                            and usdt <= RISK["max_order"]):
                        if DRY_RUN:
                            tg_send(f"🟢 <b>[模拟] 买入信号</b>\n"
                                    f"价格 {p:,.0f} | {CFG['sz']} BTC ≈ {usdt:.2f} USDT")
                        else:
                            # TODO: 接入 OKX 下单（跑通 DRY_RUN 后替换完整版）
                            pass
                        CFG["pos"] = round(CFG["pos"] + CFG["sz"], 6)
                        CFG["last_ts"] = now
                    # 卖出：涨幅达标 + 有持仓
                    elif dev >= CFG["rally_pct"] and ok_cd and CFG["pos"] >= CFG["sz"]:
                        if DRY_RUN:
                            tg_send(f"🔴 <b>[模拟] 卖出信号</b>\n"
                                    f"价格 {p:,.0f} | {CFG['sz']} BTC ≈ {usdt:.2f} USDT")
                        else:
                            pass
                        CFG["pos"] = round(CFG["pos"] - CFG["sz"], 6)
                        CFG["last_ts"] = now
        except Exception as e:
            tg_send(f"⚠️ 引擎异常: {e}")
        time.sleep(10)

# ---------- Telegram 长轮询 ----------
def tg_poll_loop():
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates",
                             params={"timeout": 25, "offset": offset + 1},
                             timeout=30).json()
            for u in r.get("result", []):
                offset = u["update_id"]
                if "callback_query" in u:
                    cq = u["callback_query"]
                    if str(cq["message"]["chat"]["id"]) == CHAT_ID:
                        on_callback(cq["data"], cq["message"]["message_id"])
                    try:
                        requests.post(f"{TG}/answerCallbackQuery",
                                      json={"callback_query_id": cq["id"]}, timeout=10)
                    except Exception:
                        pass
                elif "message" in u:
                    m = u["message"]
                    if str(m.get("chat", {}).get("id")) == CHAT_ID and m.get("text"):
                        on_text(m["text"])
        except Exception:
            time.sleep(3)

# ---------- Flask 健康端点（Render 必需） ----------
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok", "running": CFG["running"],
                    "pos": CFG["pos"], "dry_run": DRY_RUN})

if __name__ == "__main__":
    tg_send("🤖 OKX 策略管家已启动（DRY_RUN 模式）\n发送 /menu 打开控制面板")
    threading.Thread(target=strategy_loop, daemon=True).start()
    threading.Thread(target=tg_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
