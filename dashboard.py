"""
dashboard.py - 独立 Web 可视化仪表盘 (Streamlit)
运行方式: streamlit run dashboard.py --server.port 8501
"""
import sqlite3
import pandas as pd
import plotly.express as px
import streamlit as st
from datetime import datetime, timezone, timedelta

st.set_page_config(page_title="量化仪表盘", page_icon="📊", layout="wide")
st.title("🤖 量化网格机器人 实时监控")

DB_FILE = "bot.db"
CST = timezone(timedelta(hours=8))


def load_data():
    conn = sqlite3.connect(DB_FILE)
    details = pd.read_sql("SELECT * FROM trade_details ORDER BY id DESC", conn)
    config = pd.read_sql("SELECT * FROM config", conn)
    conn.close()
    return details, config


details, config = load_data()

# 侧边栏：当前参数
st.sidebar.subheader("⚙️ 当前参数")
for _, row in config.iterrows():
    st.sidebar.text(f"{row['key']}: {row['value']}")

# 主区域
col1, col2, col3 = st.columns(3)
if not details.empty:
    # 最近平仓
    sells = details[details['side'] == 'sell'].head(20)
    total_pnl = sells['pnl_pct'].sum() if not sells.empty else 0
    win_rate = (sells['pnl_pct'] > 0).mean() if not sells.empty else 0
    col1.metric("总盈亏%", f"{total_pnl:+.2f}%")
    col2.metric("胜率", f"{win_rate:.0%}")
    col3.metric("交易次数", len(sells))

    # 盈亏曲线
    if not sells.empty:
        sells['cum_pnl'] = sells['pnl_pct'].cumsum()
        fig = px.line(sells[::-1], x='time', y='cum_pnl', title="累计盈亏曲线")
        st.plotly_chart(fig, use_container_width=True)

    # 最近交易
    st.subheader("📜 最近平仓记录")
    st.dataframe(sells[['time', 'symbol', 'pnl_pct']].head(10))
else:
    st.info("暂无交易数据")