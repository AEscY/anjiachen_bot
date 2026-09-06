// 低吸高卖模式相关界面（L1控制台 + L2参数页 + L2历史记录页）

import { sendTelegram } from '../lib/telegram.js';
import { getState, setSession } from '../lib/state.js';

// L1-B：低吸高卖控制台
export async function handleDipPanel(env) {
  const config = await getState(env, 'DIP_CONFIG');
  const status = await getState(env, 'DIP_STATE') || 'WAITING_BUY';
  const totalProfit = await getState(env, 'DIP_PROFIT') || 0;
  const rounds = await getState(env, 'DIP_ROUNDS') || 0;

  const statusMap = {
    'WAITING_BUY': '🟡 等待低吸',
    'BUYING': '🔵 买单已挂',
    'WAITING_SELL': '🟠 等待高卖'
  };
  const dipStatus = config.running ? (statusMap[status] || '⏸ 已暂停') : '⏸ 已暂停';

  const spread = (((config.targetSellPrice - config.targetBuyPrice) / config.targetBuyPrice) * 100).toFixed(2);
  const expectedProfit = ((config.targetSellPrice - config.targetBuyPrice) * (config.amount / config.targetBuyPrice)).toFixed(2);
  const avgPerRound = rounds > 0 ? (totalProfit / rounds).toFixed(2) : '0';

  const text =
`🎯 <b>低吸高卖策略控制台</b>

运行状态：${dipStatus}
交易对：${config.instId}
目标买价：${config.targetBuyPrice.toLocaleString()} USDT
目标卖价：${config.targetSellPrice.toLocaleString()} USDT
投入金额：${config.amount} USDT
价差空间：+${spread}%
预期单轮利润：+${expectedProfit} USDT
─────────────────────
完成轮次：${rounds} 轮
本轮状态：${config.running ? (statusMap[status] || '—') : '已暂停'}
累计收益：+${totalProfit} USDT
平均单轮：+${avgPerRound} USDT`;

  const keyboard = [
    [
      { text: config.running ? '⏸ 暂停' : '▶️ 启动', callback_data: config.running ? 'action:dip_stop' : 'action:dip_start' },
      { text: '🔄 重置本轮', callback_data: 'action:dip_reset' }
    ],
    [
      { text: '✏️ 修改参数', callback_data: 'page:dip_params' },
      { text: '📋 历史记录', callback_data: 'page:dip_history' }
    ],
    [{ text: '🔙 返回主菜单', callback_data: 'page:main' }]
  ];

  await setSession(env, { current_page: 'dip_panel', previous_page: 'main' });
  await sendTelegram(env, text, keyboard);
}

// L2-3：低吸高卖参数修改页
export async function handleDipParams(env) {
  const config = await getState(env, 'DIP_CONFIG');

  const text =
`✏️ <b>修改低吸高卖参数</b>

当前参数：
目标买价：${config.targetBuyPrice.toLocaleString()} USDT
目标卖价：${config.targetSellPrice.toLocaleString()} USDT
投入金额：${config.amount} USDT
交易对：${config.instId}

── 点击修改哪个参数 ──
💡 点击后发送新值即可
⚠️ 修改参数会重置当前轮次`;

  const keyboard = [
    [
      { text: `目标买价 ${config.targetBuyPrice}`, callback_data: 'param:dip_buy' },
      { text: `目标卖价 ${config.targetSellPrice}`, callback_data: 'param:dip_sell' }
    ],
    [
      { text: `投入金额 ${config.amount}`, callback_data: 'param:dip_amount' }
    ],
    [
      { text: '✅ 保存并重启', callback_data: 'action:dip_start' },
      { text: '🔙 返回低吸高卖', callback_data: 'page:dip_panel' }
    ]
  ];

  await setSession(env, { current_page: 'dip_params', previous_page: 'dip_panel' });
  await sendTelegram(env, text, keyboard);
}

// L2-4：低吸高卖历史记录页
export async function handleDipHistory(env, page = 1) {
  const perPage = 5;
  const history = JSON.parse(await env.KV.get('DIP_FILLS') || '[]');

  const totalPages = Math.max(1, Math.ceil(history.length / perPage));
  const start = (page - 1) * perPage;
  const pageItems = history.slice(start, start + perPage);

  let records = '📋 <b>低吸高卖历史记录</b>\n\n';
  if (pageItems.length === 0) {
    records += '暂无完成记录';
  } else {
    for (const item of pageItems) {
      const time = new Date(item.time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
      const pct = (((item.sellPrice - item.buyPrice) / item.buyPrice) * 100).toFixed(1);
      records += `第${item.round}轮 | ${time}\n买入：${item.buyPrice} → 卖出：${item.sellPrice}\n利润：+${item.profit} USDT (+${pct}%)\n\n`;
    }
  }

  const navRow = [];
  if (page > 1) navRow.push({ text: '⬅️ 上一页', callback_data: `nav:dip_history:${page - 1}` });
  navRow.push({ text: `${page}/${totalPages}页`, callback_data: 'noop' });
  if (page < totalPages) navRow.push({ text: '➡️ 下一页', callback_data: `nav:dip_history:${page + 1}` });

  const keyboard = [navRow, [{ text: '🔙 返回低吸高卖控制台', callback_data: 'page:dip_panel' }]];
  await sendTelegram(env, records, keyboard);
}