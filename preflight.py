"""
环境预检。

不导入项目任何模块 —— 否则项目自身出错时本脚本也崩，
就失去了诊断意义。

必须在【末尾】绑定端口并常驻，否则 Render 认为服务未启动。
"""
import os
import socket
import sys
import time


def _mask(v: str) -> str:
    if not v:
        return "（空）"
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:4]}{'*' * 8}{v[-2:]} ({len(v)} 字符)"


def main() -> int:
    print("=" * 50)
    print("环境预检")
    print("=" * 50)
    problems = 0

    print("\n【必填变量】")
    for k in ("TG_BOT_TOKEN", "TG_CHAT_ID",
              "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        v = os.getenv(k, "").strip()
        if not v:
            print(f"  ❌ {k:16} 缺失或为空")
            problems += 1
        else:
            print(f"  ✅ {k:16} {_mask(v)}")

    print("\n【格式校验】")
    cid = os.getenv("TG_CHAT_ID", "").strip()
    if cid:
        try:
            int(cid)
            print(f"  ✅ TG_CHAT_ID 是纯数字: {cid}")
        except ValueError:
            print(f"  ❌ TG_CHAT_ID 不是纯数字: {cid!r}")
            print("     → 必须是数字 ID，@用户名 不行")
            problems += 1

    tok = os.getenv("TG_BOT_TOKEN", "").strip()
    if tok and ":" not in tok:
        print(f"  ⚠️ TG_BOT_TOKEN 缺少冒号，格式可能不对: {_mask(tok)}")
        problems += 1

    print("\n【可选变量】")
    for k, d in (("OKX_SANDBOX", "true"), ("EXCHANGE_ID", "okx"),
                 ("SYMBOLS", "SOL/USDT"), ("LOG_LEVEL", "INFO")):
        v = os.getenv(k, "").strip()
        print(f"  {'✅' if v else '· '} {k:16} {v or f'（默认 {d}）'}")

    syms = [s.strip() for s in os.getenv("SYMBOLS", "SOL/USDT").split(",") if s.strip()]
    print(f"  → 将交易: {syms}")

    print("\n【依赖】")
    for m in ("telegram", "ccxt"):
        try:
            mod = __import__(m)
            print(f"  ✅ {m} {getattr(mod, '__version__', '?')}")
        except Exception as e:
            print(f"  ❌ {m} 导入失败: {type(e).__name__}: {e}")
            problems += 1

    print("\n【网络连通性】")
    for label, url, host, port in (
        ("telegram", "api.telegram.org", "api.telegram.org", 443),
        ("okx", "www.okx.com", "www.okx.com", 443),
    ):
        try:
            socket.create_connection((host, port), timeout=8).close()
            print(f"  ✅ {label} 可达")
        except Exception as e:
            print(f"  ❌ {label} 不可达: {type(e).__name__}: {e}")
            problems += 1

    print("\n【结论】")
    if problems:
        print(f"  ❌ {problems} 个问题，修正后重新部署")
    else:
        print("  ✅ 全部通过 —— 把 Start Command 改回 python main.py")

    # 必须绑端口常驻，否则 Render 认为服务未启动
    port = int(os.getenv("PORT", "10000"))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(5)
    print(f"\n已绑定 0.0.0.0:{port}，保持存活 120 秒供查看日志")
    sys.stdout.flush()
    time.sleep(120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
