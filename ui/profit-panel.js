// 收益总览页面

import { sendTelegram } from '../lib/telegram.js';
import { getState, setSession } from '../lib/state.js';

export async function handleProfitPanel(env) {
  const gridProfit = await getState(env, 'GRID_PROFIT') || 0;
  const gridTodayProfit = await getState(env, 'GRID_TODAY_PROFIT') || 0;
  const gridTodayCount = await getState(env, 'GRID_TODAY_COUNT') || 0;
  const dipProfit = await getState(env, 'DIP_PROFIT') || 0;
  const dipRounds = await getState(env, 'DIP_ROUNDS') || 0;
  const avgPerRound = dipRounds > 0 ? (dipProfit / dipRounds).toFixed(2) : '0';
  const totalProfit = (gridProfit + dipProfit).toFixed(2);
  const totalToday = (gridTodayProfit).toFixed(2);

  const text =
`📈 <b>收益总览</b>

┌──── 网格策略 ────────────────┐
│ 今日收益：+${gridTodayProfit} USDT
│ 累计收益：+${gridProfit} USDT
│ 累计成交：${gridTodayCount} 次
└──────────────────────────────┘

┌──── 低吸高卖 ────────────────┐
│ 累计收益：+${dipProfit} USDT
│ 完成轮次：${dipRounds} 轮
│ 平均单轮：+${avgPerRound} USDT
└──────────────────────────────┘

┌──── 合计 ────────────────────┐
│ 今日总收益：+${totalToday} USDT
│ 历史总收益：+${totalProfit} USDT
└──────────────────────────────┘`;

  const keyboard = [
    [{ text: '🔙 返回主菜单', callback_data: 'page:main' }]
  ];
  await setSession(env, { current_page: 'profit', previous_page: 'main' });
  await sendTelegram(env, text, keyboard);
}