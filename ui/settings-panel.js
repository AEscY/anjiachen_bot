// 系统设置相关界面（L1设置 + L2切换模式 + L2通知 + L2帮助）

import { sendTelegram } from '../lib/telegram.js';
import { getState, getActiveMode, setSession } from '../lib/state.js';

// L1-C：系统设置
export async function handleSettings(env) {
  const mode = await getActiveMode(env);
  const modeLabel = { GRID: '仅网格模式', DIP_SELL: '仅低吸高卖', BOTH: '双模式并行' };
  const notifyLevel = await getState(env, 'NOTIFICATION_LEVEL') || 'ALL';
  const notifyLabel = { SILENT: '静默模式', TRADE: '仅成交通知', ALL: '全部通知' };

  const text =
`⚙️ <b>系统设置</b>

当前运行模式：${modeLabel[mode] || mode}
网格调度频率：每 5 分钟
低吸高卖频率：每 10 分钟
通知级别：${notifyLabel[notifyLevel] || notifyLevel}`;

  const keyboard = [
    [{ text: '🔀 切换运行模式', callback_data: 'page:switch_mode' }],
    [{ text: '🔔 通知偏好设置', callback_data: 'page:notify_settings' }],
    [{ text: '🛡️ API 安全检查', callback_data: 'action:api_check' }],
    [{ text: '❓ 使用帮助', callback_data: 'page:help' }],
    [{ text: '🔙 返回主菜单', callback_data: 'page:main' }]
  ];

  await setSession(env, { current_page: 'settings', previous_page: 'main' });
  await sendTelegram(env, text, keyboard);
}

// L2-5：模式切换页
export async function handleSwitchMode(env) {
  const mode = await getActiveMode(env);

  const text =
`🔀 <b>切换运行模式</b>

当前模式：${mode === 'GRID' ? '仅网格' : mode === 'DIP_SELL' ? '仅低吸高卖' : '双模式并行'}

┌─────────────────────────┐
│ 📊 仅网格模式${mode === 'GRID' ? ' ✅ 当前' : ''}
│ 只运行网格策略，低吸高卖暂停
└─────────────────────────┘

┌─────────────────────────┐
│ 🎯 仅低吸高卖模式${mode === 'DIP_SELL' ? ' ✅ 当前' : ''}
│ 只运行低吸高卖，网格暂停
└─────────────────────────┘

┌─────────────────────────┐
│ ⚡ 双模式并行${mode === 'BOTH' ? ' ✅ 当前' : ''}
│ 两个策略同时运行
└─────────────────────────┘

⚠️ 切换后暂停的策略将停止挂单
⚠️ 已有持仓不会被自动平仓`;

  const keyboard = [
    [{ text: '📊 仅网格' + (mode === 'GRID' ? ' ✅' : ''), callback_data: 'action:switch_grid' }],
    [{ text: '🎯 仅低吸高卖' + (mode === 'DIP_SELL' ? ' ✅' : ''), callback_data: 'action:switch_dip' }],
    [{ text: '⚡ 双模式并行' + (mode === 'BOTH' ? ' ✅' : ''), callback_data: 'action:switch_both' }],
    [{ text: '🔙 返回系统设置', callback_data: 'page:settings' }]
  ];

  await setSession(env, { current_page: 'switch_mode', previous_page: 'settings' });
  await sendTelegram(env, text, keyboard);
}

// L2-6：通知偏好设置
export async function handleNotifySettings(env) {
  const level = await getState(env, 'NOTIFICATION_LEVEL') || 'ALL';

  const text =
`🔔 <b>通知偏好设置</b>

当前级别：${level === 'SILENT' ? '静默模式' : level === 'TRADE' ? '仅成交通知' : '全部通知'}`;

  const keyboard = [
    [{ text: '🔕 静默模式' + (level === 'SILENT' ? ' ✅' : ''), callback_data: 'action:notify_silent' }],
    [{ text: '📬 仅成交通知' + (level === 'TRADE' ? ' ✅' : ''), callback_data: 'action:notify_trade' }],
    [{ text: '📢 全部通知' + (level === 'ALL' ? ' ✅' : ''), callback_data: 'action:notify_all' }],
    [{ text: '🔙 返回系统设置', callback_data: 'page:settings' }]
  ];

  await setSession(env, { current_page: 'notify_settings', previous_page: 'settings' });
  await sendTelegram(env, text, keyboard);
}

// L2-7：使用帮助
export async function handleHelp(env) {
  const text =
`❓ <b>使用帮助</b>

📊 <b>网格策略</b>
在设定价格区间内自动低买高卖
适合震荡行情，赚取网格差价

🎯 <b>低吸高卖</b>
在目标价自动买入，达到止盈价卖出
适合判断回调后的反弹行情

⚡ <b>双模式并行</b>
两个策略同时运行，互不干扰
注意：需确保资金充足覆盖两个策略

── 快捷指令 ──
/menu — 打开主菜单
/status — 快速查看状态
/switch grid|dip|both — 切换模式
/grid_start — 启动网格
/grid_stop — 暂停网格
/dip_start — 启动低吸高卖
/dip_stop — 暂停低吸高卖`;

  const keyboard = [
    [{ text: '🔙 返回系统设置', callback_data: 'page:settings' }]
  ];
  await setSession(env, { current_page: 'help', previous_page: 'settings' });
  await sendTelegram(env, text, keyboard);
}