# app.py — OKX 震荡套利机器人 · 自动买卖完整版
# 模式1: 网格震荡套利(自动买/卖)  模式2: 低吸高卖
import os, time, json, hmac, base64, hashlib, threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY    = os.environ.get("OKX_API_KEY", "")
SECRET     = os.environ.get("OKX_SECRET_KEY", "")
PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
DRY_RUN    = os.environ.get("DRY_RUN", "1") == "1"
TG, OKX = f"https://api.telegram.org/bot{TG_TOKEN}", "https://www.okx.com"
FEE = 0.002  # 市价单双边手续费估算

def log(*a): print(datetime.now().strftime("%d日%H:%M:%S"), *a, flush=True)

# ================= Telegram =================
def tg_send(text, kb=None, edit=None):
    p = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = {"inline_keyboard": kb}
    try:
        if edit:
            p["message_id"] = edit
            requests.post(f"{TG}/editMessageText", json=p, timeout=15)
        else:
            requests.post(f"{TG}/sendMessage", json=p, timeout=15)
    except Exception as e: log("tg:", e)

def btn(t, d): return {"text": t, "callback_data": d}

# ================= OKX V5 签名请求 =================
def _sign(ts, m, path, body):
    msg = (ts + m + path + (body or "")).encode()
    return base64.b64encode(
        hmac.new(SECRET.encode(), msg, hashlib.sha256).digest()).decode()

def okx(method, path, params=None, body=None):
    if not API_KEY: return {"code": "-2", "msg": "未配置OKX API"}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    body_s = json.dumps(body) if body else ""
    full = path + (f"?{qs}" if qs else "")
    headers = {"OK-ACCESS-KEY": API_KEY,
               "OK-ACCESS-SIGN": _sign(ts, method, full, body_s),
               "OK-ACCESS-TIMESTAMP": ts,
               "OK-ACCESS-PASSPHRASE": PASSPHRASE,
               "Content-Type": "application/json"}
    for _ in range(3):
        try:
            r = requests.request(method, OKX + full, headers=headers,
                                 data=body_s or None, timeout=15)
            return r.json()
        except Exception: time.sleep(2)
    return {"code": "-1", "msg": "网络错误"}

def place_market(inst, side, sz):
    """市价单：保证成交，网格账本不漂移"""
    if DRY_RUN:
        return {"code": "0", "ordId": f"DRY{int(time.time()*1000)}", "dry": True}
    r = okx("POST", "/api/v5/trade/order", body={
        "instId": inst, "tdMode": "cash", "side": side,
        "ordType": "market", "sz": str(sz)})
    if r.get("code") == "0" and r.get("data"):
        r["ordId"] = r["data"][0].get("ordId", "")
    return r

def order_fill(inst, ord_id):
    try:
        r = okx("GET", "/api/v5/trade/order",
                params={"instId": inst, "ordId": ord_id})
        d = r["data"][0]
        return float(d["avgPx"]), abs(float(d.get("fee") or 0))
    except Exception:
        return None, 0.0

def get_price(inst):
    try:
        r = requests.get(f"{OKX}/api/v5/market/ticker?instId={inst}",
                         timeout=10).json()
        if r.get("code") == "0": return float(r["data"][0]["last"])
    except Exception: pass
    return None

def get_bal(ccy):
    try:
        r = okx("GET", "/api/v5/account/balance", params={"ccy": ccy})
        for d in r["data"][0]["details"]:
            if d["ccy"] == ccy: return float(d.get("availBal") or 0)
    except Exception: pass
    return 0.0

# ================= 模式1: 网格震荡套利 =================
GRID = {
    "instId": "BTC-USDT",
    "lo": None, "hi": None,      # 价格区间
    "n": 10,                     # 格数
    "sz": 0.0001,                # 每格数量(BTC)
    "max_inv": 10,               # 最大持货格数
    "running": False,
    # 运行时
    "step": None, "last_iv": None,
    "inv": {},                   # {格线号: 数量}
    "rounds": 0, "gross": 0.0, "fees": 0.0, "recs": [],
}

def _iv(p):  # 价格所在区间号
    return max(0, min(GRID["n"], int((p - GRID["lo"]) / GRID["step"])))

def _grid_rebase():
    GRID["step"] = (GRID["hi"] - GRID["lo"]) / GRID["n"]
    GRID["inv"], GRID["last_iv"] = {}, None
    GRID["rounds"], GRID["gross"], GRID["fees"] = 0, 0.0, 0.0

def grid_start():
    if not GRID["lo"] or not GRID["hi"]:
        tg_send("⚠️ 请先设置价格区间（点 ⚡区间±10%）")
        return False
    _grid_rebase()
    p = get_price(GRID["instId"])
    if p is None: return False
    GRID["last_iv"] = _iv(p)
    if not DRY_RUN:  # 以交易所为真实状态源：同步已有BTC
        bal = get_bal("BTC")
        units = min(int(bal / GRID["sz"]), GRID["max_inv"])
        for i in range(units):
            line = max(0, GRID["last_iv"] - 1 - i)
            GRID["inv"][line] = GRID["inv"].get(line, 0) + 1
        if units:
            tg_send(f"📥 已同步账户持货 {units} 格（挂当前价下方格线）")
    GRID["running"] = True
    tg_send(f"✅ <b>网格套利启动</b>\n"
            f"区间 {GRID['lo']:,.0f} ~ {GRID['hi']:,.0f} · {GRID['n']}格\n"
            f"格距 ≈{GRID['step']:,.0f} ({GRID['step']/p*100:.2f}%)\n"
            f"每格 {GRID['sz']} BTC · 最大持货 {GRID['max_inv']} 格")
    return True

def _g_trade(side, line, px):
    r = place_market(GRID["instId"], side, GRID["sz"])
    if r.get("code") != "0" or not r.get("ordId"):
        GRID["running"] = False
        tg_send(f"❌ {side} 下单失败，网格已自动暂停\n{r.get('msg','')}")
        return False
    if r.get("dry"):
        avg, fee = px, px * GRID["sz"] * FEE
    else:
        avg, fee = order_fill(GRID["instId"], r["ordId"])
        avg = avg or px
    GRID["recs"].append({"t": time.time(), "side": side, "px": avg,
                         "sz": GRID["sz"], "fee": fee})
    GRID["fees"] += fee
    if side == "buy":
        GRID["inv"][line] = GRID["inv"].get(line, 0) + 1
        tg_send(f"🟢 <b>网格买入</b> 第{line}格\n"
                f"{GRID['sz']} BTC @ {avg:,.0f} (≈{avg*GRID['sz']:.1f}U)")
    else:
        GRID["inv"][line] = GRID["inv"].get(line, 0) - 1
        GRID["rounds"] += 1
        GRID["gross"] += GRID["step"]
        tg_send(f"🔴 <b>网格卖出</b> 完成第{GRID['rounds']}回合\n"
                f"{GRID['sz']} BTC @ {avg:,.0f} | 回合毛利≈{GRID['step']:,.0f}")
    return True

def grid_tick():
    if not GRID["running"] or GRID["step"] is None: return
    p = get_price(GRID["instId"])
    if p is None: return
    if GRID["last_iv"] is None:
        GRID["last_iv"] = _iv(p); return
    cur, last, trades = _iv(p), GRID["last_iv"], 0
    if cur == last: return
    while last > cur and trades < 3:      # 跌穿格线 → 买入该格
        line = last
        if GRID["inv"].get(line, 0) == 0 \
           and sum(GRID["inv"].values()) < GRID["max_inv"]:
            if _g_trade("buy", line, p): trades += 1
        last -= 1
    while cur > last and trades < 3:      # 升穿格线 → 卖出下格的货
        below = last
        if GRID["inv"].get(below, 0) > 0:
            if _g_trade("sell", below, p): trades += 1
        last += 1
    GRID["last_iv"] = cur  # 一次性穿过3格以上时留待下轮，防爆单

# ================= 模式2: 低吸高卖 =================
SW = {"instId": "BTC-USDT", "base": None, "dip": 0.02, "up": 0.03,
      "sz": 0.0001, "max_pos": 0.001, "cd": 300, "running": False,
      "pos": 0.0, "last": 0, "recs": []}

def swing_tick():
    if not SW["running"]: return
    p = get_price(SW["instId"])
    if p is None: return
    if SW["base"] is None: SW["base"] = p; return
    dev = (p - SW["base"]) / SW["base"]
    if time.time() - SW["last"] < SW["cd"]: return
    if dev <= -SW["dip"] and SW["pos"] + SW["sz"] <= SW["max_pos"]:
        r = place_market(SW["instId"], "buy", SW["sz"])
        if r.get("code") == "0":
            SW["pos"] = round(SW["pos"] + SW["sz"], 6)
            SW["last"] = time.time(); SW["base"] = p
            avg = p if r.get("dry") else (order_fill(SW["instId"], r["ordId"])[0] or p)
            SW["recs"].append({"t": time.time(), "side": "buy", "px": avg, "sz": SW["sz"]})
            tg_send(f"🟢 <b>低吸入场</b> {SW['sz']} BTC @ {avg:,.0f}")
    elif dev >= SW["up"] and SW["pos"] >= SW["sz"]:
        r = place_market(SW["instId"], "sell", SW["sz"])
        if r.get("code") == "0":
            SW["pos"] = round(SW["pos"] - SW["sz"], 6)
            SW["last"] = time.time(); SW["base"] = p
            avg = p if r.get("dry") else (order_fill(SW["instId"], r["ordId"])[0] or p)
            SW["recs"].append({"t": time.time(), "side": "sell", "px": avg, "sz": SW["sz"]})
            tg_send(f"🔴 <b>高抛离场</b> {SW['sz']} BTC @ {avg:,.0f}")

# ================= Telegram 页面 =================
def page_home():
    p = get_price(GRID["instId"])
    units = sum(GRID["inv"].values())
    t = (f"🤖 <b>OKX 震荡套利管家</b> 🟢\n\n"
         f"📊 BTC: {p:,.0f}\n\n"
         f"📐 网格套利: {'▶️运行' if GRID['running'] else '⏸️停'} · "
         f"回合 {GRID['rounds']} · 净利≈{GRID['gross']-GRID['fees']:+.2f}U\n"
         f"📉 低吸高卖: {'▶️运行' if SW['running'] else '⏸️停'} · 持仓 {SW['pos']:.4f} BTC\n\n"
         f"🧪 {'模拟模式(不下真单)' if DRY_RUN else '⚠️ 实盘模式'}")
    kb = [[btn("📐 网格套利", "grid"), btn("📉 低吸高卖", "dip")],
          [btn("🔄 刷新", "home")]]
    return t, kb

def page_grid():
    p = get_price(GRID["instId"])
    if GRID["step"]:
        units = sum(GRID["inv"].values())
        t = (f"📐 <b>网格震荡套利</b> "
             f"{'▶️运行' if GRID['running'] else '⏸️暂停'}\n\n"
             f"区间: {GRID['lo']:,.0f} ~ {GRID['hi']:,.0f} · {GRID['n']}格\n"
             f"格距: {GRID['step']:,.0f} ({GRID['step']/p*100:.2f}%)\n"
             f"当前: {p:,.0f} (第{_iv(p)}格)\n\n"
             f"持货: {units}格 · {units*GRID['sz']:.4f} BTC "
             f"≈{units*GRID['sz']*p:,.1f}U / 上限{GRID['max_inv']}格\n"
             f"完成回合: {GRID['rounds']} · 每回合毛利≈{GRID['step']:,.0f}\n"
             f"估算净利: <b>{GRID['gross']-GRID['fees']:+.2f} USDT</b>"
             f"(费{GRID['fees']:.2f})\n\n"
             f"规则: 跌穿格线买一格 · 升穿格线卖一格")
    else:
        t = ("📐 <b>网格震荡套利</b> ⏸️ 未配置\n\n"
             "先设置价格区间（下方快捷按钮）\n"
             "或发文本 <code>95000,115000,10,0.0001,10</code>")
    kb = []
    if GRID["step"]:
        kb.append([btn("⏸️ 暂停" if GRID["running"] else "▶️ 启动", "g_tog")])
    kb += [[btn("⚡区间±5%", "g_r5"), btn("⚡区间±10%", "g_r10")],
           [btn("⚙️ 参数", "g_par"), btn("📜 记录", "g_rec")],
           [btn("⬅️ 主页", "home")]]
    return t, kb

def page_g_par():
    p = get_price(GRID["instId"]) or 0
    need = GRID["max_inv"] * GRID["sz"] * (GRID["lo"] or p)
    t = (f"⚙️ <b>网格参数</b>\n\n"
         f"下限 {GRID['lo'] or '未设'} · 上限 {GRID['hi'] or '未设'}\n"
         f"格数 {GRID['n']} · 每格 {GRID['sz']} BTC (≈{GRID['sz']*p:,.1f}U)\n"
         f"最大持货 {GRID['max_inv']} 格\n\n"
         f"💰 预备资金 ≈ <b>{need:,.0f} USDT</b> (最坏跌满时的买入总额)\n\n"
         f"文本一键设置:\n"
         f"<code>95000,115000,10,0.0001,10</code>\n"
         f"(下限,上限,格数,每格BTC,最大格数)\n\n"
         f"⚠️ 改参数会重置账本并暂停")
    kb = [[btn("格数−1", "g_adj:n:-1"), btn("格数+1", "g_adj:n:1")],
          [btn("每格−", "g_adj:sz:-0.00005"), btn("每格+", "g_adj:sz:0.00005")],
          [btn("持货−1", "g_adj:max_inv:-1"), btn("持货+1", "g_adj:max_inv:1")],
          [btn("⬅️ 返回", "grid")]]
    return t, kb

def _recs_page(recs, title, back):
    rows = recs[-8:][::-1]
    body = "\n".join(
        f"{time.strftime('%d日%H:%M', time.localtime(r['t']))} "
        f"{'🟢买' if r['side']=='buy' else '🔴卖'} "
        f"{r['sz']} @ {r['px']:,.0f}" for r in rows) or "暂无"
    return f"📜 <b>{title}</b>\n\n{body}", [[btn("⬅️ 返回", back)]]

def page_dip():
    p = get_price(SW["instId"]) or 0
    base = SW["base"] or p
    t = (f"📉 <b>低吸高卖</b> {'▶️运行' if SW['running'] else '⏸️暂停'}\n\n"
         f"当前 {p:,.0f} · 基准 {base:,.0f}\n"
         f"买入: 跌破 {base*(1-SW['dip']):,.0f} (-{SW['dip']*100:.1f}%)\n"
         f"卖出: 涨破 {base*(1+SW['up']):,.0f} (+{SW['up']*100:.1f}%)\n"
         f"持仓: {SW['pos']:.4f}/{SW['max_pos']:.4f} BTC\n\n"
         f"文本设置: <code>2.5,3.5,0.0002,0.002,600</code>\n"
         f"(跌%,涨%,单笔BTC,最大持仓,冷却秒)")
    kb = [[btn("⏸️ 暂停" if SW["running"] else "▶️ 启动", "d_tog")],
          [btn("📜 记录", "d_rec"), btn("⬅️ 主页", "home")]]
    return t, kb

PAGES = {"home": page_home, "grid": page_grid, "g_par": page_g_par,
         "dip": page_dip}

# ================= 回调路由 =================
def on_cb(data, mid):
    if data in PAGES:
        t, kb = PAGES[data](); tg_send(t, kb, edit=mid)
    elif data == "g_tog":
        if GRID["running"]:
            GRID["running"] = False
            t, kb = page_grid(); tg_send(t, kb, edit=mid)
        elif grid_start():
            t, kb = page_grid(); tg_send(t, kb, edit=mid)
    elif data in ("g_r5", "g_r10"):
        pct = 0.05 if data == "g_r5" else 0.10
        p = get_price(GRID["instId"])
        if p:
            GRID["running"] = False
            GRID["lo"], GRID["hi"] = round(p*(1-pct)), round(p*(1+pct))
            _grid_rebase()
            t, kb = page_grid(); tg_send(t, kb, edit=mid)
    elif data.startswith("g_adj:"):
        _, k, d = data.split(":"); d = float(d)
        GRID["running"] = False
        if k == "n": GRID["n"] = max(3, min(50, GRID["n"] + int(d)))
        elif k == "sz": GRID["sz"] = max(0.00001, round(GRID["sz"] + d, 6))
        elif k == "max_inv":
            GRID["max_inv"] = max(1, min(50, GRID["max_inv"] + int(d)))
        if GRID["lo"]: _grid_rebase()
        t, kb = page_g_par(); tg_send(t, kb, edit=mid)
    elif data == "g_rec":
        t, kb = _recs_page(GRID["recs"], "网格交易记录", "grid")
        tg_send(t, kb, edit=mid)
    elif data == "d_tog":
        SW["running"] = not SW["running"]
        if SW["running"] and SW["base"] is None:
            p = get_price(SW["instId"])
            if p: SW["base"] = p
        t, kb = page_dip(); tg_send(t, kb, edit=mid)
    elif data == "d_rec":
        t, kb = _recs_page(SW["recs"], "低吸高卖记录", "dip")
        tg_send(t, kb, edit=mid)

# ================= 文本输入 =================
def on_text(txt):
    if txt in ("/menu", "/start"):
        t, kb = page_home(); tg_send(t, kb); return
    try:
        parts = [x.strip() for x in txt.replace("，", ",").split(",")]
        v = [float(x) for x in parts]
        if len(v) == 5 and v[0] > 1000:      # 网格五元组
            GRID.update(running=False, lo=v[0], hi=v[1],
                        n=int(v[2]), sz=v[3], max_inv=int(v[4]))
            _grid_rebase()
            tg_send("✅ 网格参数已保存（账本已重置，需手动启动）")
            t, kb = page_grid(); tg_send(t, kb)
        elif len(v) == 5:                     # 低吸高卖五元组
            SW.update(dip=max(0.005, v[0]/100), up=max(0.005, v[1]/100),
                      sz=max(0.00001, v[2]), max_pos=max(v[2], v[3]),
                      cd=max(60, int(v[4])))
            tg_send("✅ 低吸高卖参数已保存")
            t, kb = page_dip(); tg_send(t, kb)
        else:
            tg_send("💡 需要5个数字，用逗号分隔。发送 /menu")
    except Exception:
        tg_send("❌ 格式错误。示例:\n网格 <code>95000,115000,10,0.0001,10</code>\n"
                "低吸 <code>2.5,3.5,0.0002,0.002,600</code>")

# ================= 主循环 =================
def engine_loop():
    while True:
        try:
            grid_tick(); swing_tick()
        except Exception as e:
            log("engine:", e)
        time.sleep(10)  # 10秒一轮，远低于限流

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
                        on_cb(cq["data"], cq["message"]["message_id"])
                    try:
                        requests.post(f"{TG}/answerCallbackQuery",
                                      json={"callback_query_id": cq["id"]},
                                      timeout=10)
                    except Exception: pass
                elif "message" in u:
                    m = u["message"]
                    if str(m.get("chat", {}).get("id")) == CHAT_ID and m.get("text"):
                        on_text(m["text"])
        except Exception:
            time.sleep(3)

app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok", "dry_run": DRY_RUN,
                    "grid": {"running": GRID["running"],
                             "rounds": GRID["rounds"],
                             "units": sum(GRID["inv"].values())},
                    "swing": {"running": SW["running"], "pos": SW["pos"]}})

if __name__ == "__main__":
    tg_send("🤖 <b>震荡套利机器人已启动</b>\n"
            f"模式: {'🧪模拟(不下真单)' if DRY_RUN else '⚠️实盘'}\n"
            "发送 /menu 打开控制面板")
    threading.Thread(target=engine_loop, daemon=True).start()
    threading.Thread(target=tg_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
