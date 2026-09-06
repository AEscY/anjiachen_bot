// L0 主菜单页面渲染

import { sendTelegram } from '../lib/telegram.js';
import { getState, getActiveMode, setSession } from '../lib/state.js';

export async function handleMenuPage(env) {
  const mode = await getActiveMode(env);
  const gridConfig = await getState(env, 'GRID_CONFIG');
  const dipConfig = await getState(env, 'DIP_CONFIG');
  const gridTodayProfit = await getState(env, 'GRID_TODAY_PROFIT') || 0;
  const gridTodayCount = await getState(env, 'GRID_TODAY_COUNT') || 0;
  const dipProfit = await getState(env, 'DIP_PROFIT') || 0;
  const dipRounds = await getState(env, 'DIP_ROUNDS') || 0;

  const modeLabel = { GRID: '仅网格', DIP_SELL: '仅低吸高卖', BOTH: '双模式并行' };

  const gridStatus = gridConfig.running ? '🟢 运行中' : '⏸ 已暂停';
  const dipStatus = dipConfig.running ? '🟢 运行中' : '⏸ 已暂停';

  const text =
`🤖 <b>OKX 策略控制中心</b>

📡 系统状态：在线
🔥 当前模式：${modeLabel[mode] || mode}

┌─────────────────────────┐
│ 📊 网格策略    ${gridStatus}
│ 今日：+${gridTodayProfit} USDT | 成交 ${gridTodayCount} 次
└─────────────────────────┘

┌─────────────────────────┐
│ 🎯 低吸高卖    ${dipStatus}
│ 累计：+${dipProfit} USDT | ${dipRounds} 轮完成
└─────────────────────────┘`;

  const keyboard = [
    [{ text: '📊 网格控制台', callback_data: 'page:grid_panel' }, { text: '🎯 低吸高卖', callback_data: 'page:dip_panel' }],
    [{ text: '⚙️ 系统设置', callback_data: 'page:settings' }, { text: '📈 收益总览', callback_data: 'page:profit' }]
  ];

  await setSession(env, { current_page: 'main', previous_page: null });
  await sendTelegram(env, text, keyboard);
}