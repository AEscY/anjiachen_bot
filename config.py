import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # ----- 交易所基础 -----
    OKX_API_KEY = os.getenv("OKX_API_KEY")
    OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
    OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
    IS_SANDBOX = os.getenv("IS_SANDBOX", "true").lower() == "true"
    SYMBOL = os.getenv("SYMBOL", "BTC-USDT")
    
    # ----- 电报通知 -----
    TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TG_CHAT_ID")
    ALLOWED_USERS = os.getenv("ALLOWED_USERS", "").split(",")

    # ----- 🚀 动态策略参数（实时自适应） -----
    BASE_GRID_NUM = 10                # 基础网格挂单层数（双边）
    GRID_VOLATILITY_SCALE = 1.5       # 波动率放大系数（ATR越大，网格拉得越宽）
    IMBALANCE_THRESHOLD = 0.25        # 订单簿失衡触发阈值（0.25即买盘/卖盘差距>25%触发抢跑）
    TRAILING_STOP_PCT = 0.005         # 移动止盈回撤比例 (0.5%)
    MAX_POSITION_USDT = 1000          # 单方向最大持仓价值（USDT）
    MIN_ORDER_USDT = 10               # 最小下单金额
    
    # ----- WebSocket 订阅配置 -----
    WS_URL = "wss://ws.okx.com:8443/ws/v5/public" if not IS_SANDBOX else "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
    
    @staticmethod
    def get_contract_info():
        # 根据交易对自动推断合约精度（后续可扩展）
        return {"instId": Config.SYMBOL, "sz_decimals": 0, "px_decimals": 1}