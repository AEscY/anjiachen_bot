"""
dashboard.py - Streamlit 仪表盘（增加自动刷新和异常处理）
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


@st.cache_data(ttl=10)
def load_data():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        details = pd.read_sql("SELECT * FROM trade_details ORDER BY id DESC LIMIT 500", conn)
        config = pd.read_sql("SELECT * FROM config", conn)
        conn.close()
        return details, config
    except Exception as e:
        st.error(f"加载数据失败: {e}")
        return pd.DataFrame(), pd.DataFrame()


details, config = load_data()

st.sidebar.subheader("⚙️ 当前参数")
if not config.empty:
    for _, row in config.iterrows():
        st.sidebar.text(f"{row['key']}: {row['value']}")
else:
    st.sidebar.info("暂无配置数据")

col1, col2, col3 = st.columns(3)
if not details.empty:
    sells = details[details['side'] == 'sell'].head(20)
    total_pnl = sells['pnl_pct'].sum() if not sells.empty else 0
    win_rate = (sells['pnl_pct'] > 0).mean() if not sells.empty else 0
    col1.metric("总盈亏%", f"{total_pnl:+.2f}%")
    col2.metric("胜率", f"{win_rate:.0%}")
    col3.metric("交易次数", len(sells))

    if not sells.empty:
        sells['cum_pnl'] = sells['pnl_pct'].cumsum()
        fig = px.line(sells[::-1], x='time', y='cum_pnl', title="累计盈亏曲线")
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("📜 最近平仓记录")
    st.dataframe(sells[['time', 'symbol', 'pnl_pct']].head(10))
else:
    st.info("暂无交易数据")

# 自动刷新
if st.button("🔄 刷新数据"):
    st.cache_data.clear()
    st.rerun()