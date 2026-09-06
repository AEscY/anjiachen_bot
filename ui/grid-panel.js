// 网格模式相关界面（L1控制台 + L2参数页 + L2成交记录页）

import { sendTelegram } from '../lib/telegram.js';
import { getState, setSession } from '../lib/state.js';

// L1-A：网格策略控制台
export async function handleGridPanel(env) {
  const config = await getState(env, 'GRID_CONFIG');
  const totalProfit = await getState(env, 'GRID_PROFIT') || 0;
  const todayProfit = await getState(env, 'GRID_TODAY_PROFIT') || 0;
  const todayCount = await getState(env, 'GRID_TODAY_COUNT') || 0;
  const gridStep = ((config.upperPrice - config.lowerPrice) / config.gridCount).toFixed(0);
  const totalAmount = (config.gridCount * config.perGridAmount).toFixed(0);

  let pendingBuys = 0, pendingSells = 0;
  try {
    const { okxRequest } = await import('../lib/okx.js');
    const pendingRes = await okxRequest(env, 'GET', '/api/v5/trade/orders-pending', { instId: config.instId });
    for (const o of (pendingRes.data || [])) {
      if (o.side === 'buy') pendingBuys++;
      if (o.side === 'sell') pendingSells++;
    }
  } catch (e) {}

  const statusIcon = config.running ? '🟢 运行中' : '⏸ 已暂停';

  const text =
`📊 <b>网格策略控制台</b>

运行状态：${statusIcon}
交易对：${config.instId}
区间：${config.lowerPrice.toLocaleString()} — ${config.upperPrice.toLocaleString()} USDT
网格数：${config.gridCount} 格 | 间距：${gridStep} USDT
每格投入：${config.perGridAmount} USDT
总投入：${totalAmount} USDT
─────────────────────
今日成交：${todayCount} 次
今日收益：+${todayProfit} USDT
累计收益：+${totalProfit} USDT
未完成挂单：买单 ${pendingBuys} | 卖单 ${pendingSells}`;

  const keyboard = [
    [
      { text: config.running ? '⏸ 暂停' : '▶️ 启动', callback_data: config.running ? 'action:grid_stop' : 'action:grid_start' },
      { text: '🔄 重新挂单', callback_data: 'action:grid_rebalance' }
    ],
    [
      { text: '✏️ 修改参数', callback_data: 'page:grid_params' },
      { text: '📋 成交记录', callback_data: 'page:grid_fills' }
    ],
    [{ text: '🔙 返回主菜单', callback_data: 'page:main' }]
  ];

  await setSession(env, { current_page: 'grid_panel', previous_page: 'main' });
  await sendTelegram(env, text, keyboard);
}

// L2-1：网格参数修改页
export async function handleGridParams(env) {
  const config = await getState(env, 'GRID_CONFIG');

  const text =
`✏️ <b>修改网格参数</b>

当前参数：
价格下限：${config.lowerPrice.toLocaleString()} USDT
价格上限：${config.upperPrice.toLocaleString()} USDT
网格数量：${config.gridCount} 格
每格金额：${config.perGridAmount} USDT
交易对：${config.instId}

── 点击修改哪个参数 ──
💡 点击后发送新值即可
⚠️ 修改后需重启策略才生效`;

  const keyboard = [
    [
      { text: `价格下限 ${config.lowerPrice}`, callback_data: 'param:grid_lower' },
      { text: `价格上限 ${config.upperPrice}`, callback_data: 'param:grid_upper' }
    ],
    [
      { text: `网格数量 ${config.gridCount}`, callback_data: 'param:grid_count' },
      { text: `每格金额 ${config.perGridAmount}`, callback_data: 'param:grid_amount' }
    ],
    [
      { text: '✅ 保存并重启', callback_data: 'action:grid_start' },
      { text: '🔙 返回网格', callback_data: 'page:grid_panel' }
    ]
  ];

  await setSession(env, { current_page: 'grid_params', previous_page: 'grid_panel' });
  await sendTelegram(env, text, keyboard);
}

// L2-2：网格成交记录页
export async function handleGridFills(env, page = 1) {
  const perPage = 5;
  let fills = [];
  try {
    const { okxRequest } = await import('../lib/okx.js');
    const config = await getState(env, 'GRID_CONFIG');
    const res = await okxRequest(env, 'GET', '/api/v5/trade/fills', { instId: config.instId, limit: '50' });
    fills = res.data || [];
  } catch (e) {}

  const totalPages = Math.max(1, Math.ceil(fills.length / perPage));
  const start = (page - 1) * perPage;
  const pageFills = fills.slice(start, start + perPage);

  let records = '📋 <b>网格成交记录</b>\n\n';
  if (pageFills.length === 0) {
    records += '暂无成交记录';
  } else {
    for (const fill of pageFills) {
      const time = new Date(parseInt(fill.ts)).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
      const side = fill.side === 'buy' ? '买入 🟢' : '卖出 🔴';
      const fee = Math.abs(parseFloat(fill.fee));
      records += `${side} | ${time}\n价格：${fill.fillPx} | 数量：${fill.fillSz}\n手续费：${fee}\n\n`;
    }
  }

  const navRow = [];
  if (page > 1) navRow.push({ text: '⬅️ 上一页', callback_data: `nav:grid_fills:${page - 1}` });
  navRow.push({ text: `${page}/${totalPages}页`, callback_data: 'noop' });
  if (page < totalPages) navRow.push({ text: '➡️ 下一页', callback_data: `nav:grid_fills:${page + 1}` });

  const keyboard = [navRow, [{ text: '🔙 返回网格控制台', callback_data: 'page:grid_panel' }]];
  await sendTelegram(env, records, keyboard);
}