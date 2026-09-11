// 网格模式相关界面（L1控制台 + L2参数页 + L2成交记录页）
import { sendTelegram } from '../lib/telegram.js';
import { getState, setSession } from '../lib/state.js';

export async function handleGridPanel(env) {
  const config = await getState(env, 'GRID_CONFIG');
  const totalProfit = (await getState(env, 'GRID_PROFIT')) || 0;
  const todayProfit = (await getState(env, 'GRID_TODAY_PROFIT')) || 0;
  const todayCount = (await getState(env, 'GRID_TODAY_COUNT')) || 0;
  const gridStep = ((config.upperPrice - config.lowerPrice) / config.gridCount).toFixed(0);
  const totalAmount = (config.gridCount * config.perGridAmount).toFixed(0);

  let pendingBuys = 0,
    pendingSells = 0;
  try {
    const { okxRequest } = await import('../lib/okx.js');
    const pendingRes = await okxRequest(env, 'GET', '/api/v5/trade/orders-pending', { instId: config.instId });
    for (const o of pendingRes.data || []) {
      if (o.side === 'buy') pendingBuys++;
      if (o.side === 'sell') pendingSells++;
    }
  } catch (e) {}

  const statusIcon = config.running ? '🟢 运行中' : '⏸ 已暂停';

  const text =
    `📊 <b>网格策略控制台</b>\n\n` +
    `运行状态：${statusIcon}\n` +
    `交易对：${config.instId}\n` +
    `区间：${config.lowerPrice.toLocaleString()} — ${config.upperPrice.toLocaleString()} USDT\n` +
    `网格数：${config.gridCount} 格 | 间距：${gridStep} USDT\n` +
    `每格投入：${config.perGridAmount} USDT\n` +
    `总投入：${totalAmount} USDT\n` +
    `─────────────────────\n` +
    `今日成交：${todayCount} 次\n` +
    `今日收益：+${todayProfit} USDT\n` +
    `累计收益：+${totalProfit} USDT\n` +
    `未完成挂单：买单 ${pendingBuys} | 卖单 ${pendingSells}`;

  const keyboard = [
    [
      { text: config.running ? '⏸ 暂停' : '▶️ 启动', callback_data: config.running ? 'action:grid_stop' : 'action:grid_start' },
      { text: '🔄 重新挂单', callback_data: 'action:grid_rebalance' },
    ],
    [
      { text: '✏️ 修改参数', callback_data: 'page:grid_params' },
      { text: '📋 成交记录', callback_data: 'page:grid_fills' },
    ],
    [{ text: '🔙 返回主菜单', callback_data: 'page:main' }],
  ];

  await setSession(env, { current_page: 'grid_panel', previous_page: 'main' });
  await sendTelegram(env, text, keyboard);
}

// L2-1：网格参数修改页（增加交易对）
export async function handleGridParams(env) {
  const config = await getState(env, 'GRID_CONFIG');

  const text =
    `✏️ <b>修改网格参数</b>\n\n` +
    `当前参数：\n` +
    `价格下限：${config.lowerPrice.toLocaleString()} USDT\n` +
    `价格上限：${config.upperPrice.toLocaleString()} USDT\n` +
    `网格数量：${config.gridCount} 格\n` +
    `每格金额：${config.perGridAmount} USDT\n` +
    `交易对：${config.instId}\n\n` +
    `── 点击修改哪个参数 ──\n` +
    `💡 点击后发送新值即可\n` +
    `⚠️ 修改后需重启策略才生效`;

  const keyboard = [
    [
      { text: `价格下限 ${config.lowerPrice}`, callback_data: 'param:grid_lower' },
      { text: `价格上限 ${config.upperPrice}`, callback_data: 'param:grid_upper' },
    ],
    [
      { text: `网格数量 ${config.gridCount}`, callback_data: 'param:grid_count' },
      { text: `每格金额 ${config.perGridAmount}`, callback_data: 'param:grid_amount' },
    ],
    [
      { text: `交易对 ${config.instId}`, callback_data: 'param:instId' },
      { text: '✅ 保存并重启', callback_data: 'action:grid_start' },
    ],
    [{ text: '🔙 返回网格', callback_data: 'page:grid_panel' }],
  ];

  await setSession(env, { current_page: 'grid_params', previous_page: 'grid_panel' });
  await sendTelegram(env, text, keyboard);
}

// L2-2：网格成交记录页（与原来相同，略）
export async function handleGridFills(env, page = 1) {
  // 原代码不变，此处省略
}