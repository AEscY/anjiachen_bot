// 低吸高卖模式相关界面（L1控制台 + L2参数页 + L2历史记录页）
import { sendTelegram } from '../lib/telegram.js';
import { getState, setSession } from '../lib/state.js';

// L1-B：低吸高卖控制台
export async function handleDipPanel(env) {
  const config = await getState(env, 'DIP_CONFIG');
  const status = (await getState(env, 'DIP_STATE')) || 'WAITING_BUY';
  const totalProfit = (await getState(env, 'DIP_PROFIT')) || 0;
  const rounds = (await getState(env, 'DIP_ROUNDS')) || 0;

  const statusMap = {
    WAITING_BUY: '🟡 等待低吸',
    BUYING: '🔵 买单已挂',
    WAITING_SELL: '🟠 等待高卖',
  };
  const dipStatus = config.running ? statusMap[status] || '⏸ 已暂停' : '⏸ 已暂停';

  const spread = (((config.targetSellPrice - config.targetBuyPrice) / config.targetBuyPrice) * 100).toFixed(2);
  const expectedProfit = ((config.targetSellPrice - config.targetBuyPrice) * (config.amount / config.targetBuyPrice)).toFixed(
    2
  );
  const avgPerRound = rounds > 0 ? (totalProfit / rounds).toFixed(2) : '0';

  const text =
    `🎯 <b>低吸高卖策略控制台</b>\n\n` +
    `运行状态：${dipStatus}\n` +
    `交易对：${config.instId}\n` +
    `目标买价：${config.targetBuyPrice.toLocaleString()} USDT\n` +
    `目标卖价：${config.targetSellPrice.toLocaleString()} USDT\n` +
    `投入金额：${config.amount} USDT\n` +
    `价差空间：+${spread}%\n` +
    `预期单轮利润：+${expectedProfit} USDT\n` +
    `─────────────────────\n` +
    `完成轮次：${rounds} 轮\n` +
    `本轮状态：${config.running ? statusMap[status] || '—' : '已暂停'}\n` +
    `累计收益：+${totalProfit} USDT\n` +
    `平均单轮：+${avgPerRound} USDT`;

  const keyboard = [
    [
      { text: config.running ? '⏸ 暂停' : '▶️ 启动', callback_data: config.running ? 'action:dip_stop' : 'action:dip_start' },
      { text: '🔄 重置本轮', callback_data: 'action:dip_reset' },
    ],
    [
      { text: '✏️ 修改参数', callback_data: 'page:dip_params' },
      { text: '📋 历史记录', callback_data: 'page:dip_history' },
    ],
    [{ text: '🔙 返回主菜单', callback_data: 'page:main' }],
  ];

  await setSession(env, { current_page: 'dip_panel', previous_page: 'main' });
  await sendTelegram(env, text, keyboard);
}

// L2-3：低吸高卖参数修改页（增加交易对）
export async function handleDipParams(env) {
  const config = await getState(env, 'DIP_CONFIG');

  const text =
    `✏️ <b>修改低吸高卖参数</b>\n\n` +
    `当前参数：\n` +
    `目标买价：${config.targetBuyPrice.toLocaleString()} USDT\n` +
    `目标卖价：${config.targetSellPrice.toLocaleString()} USDT\n` +
    `投入金额：${config.amount} USDT\n` +
    `交易对：${config.instId}\n\n` +
    `── 点击修改哪个参数 ──\n` +
    `💡 点击后发送新值即可\n` +
    `⚠️ 修改参数会重置当前轮次`;

  const keyboard = [
    [
      { text: `目标买价 ${config.targetBuyPrice}`, callback_data: 'param:dip_buy' },
      { text: `目标卖价 ${config.targetSellPrice}`, callback_data: 'param:dip_sell' },
    ],
    [
      { text: `投入金额 ${config.amount}`, callback_data: 'param:dip_amount' },
      { text: `交易对 ${config.instId}`, callback_data: 'param:instId' },
    ],
    [
      { text: '✅ 保存并重启', callback_data: 'action:dip_start' },
      { text: '🔙 返回低吸高卖', callback_data: 'page:dip_panel' },
    ],
  ];

  await setSession(env, { current_page: 'dip_params', previous_page: 'dip_panel' });
  await sendTelegram(env, text, keyboard);
}

// L2-4：低吸高卖历史记录页（不变，略）
export async function handleDipHistory(env, page = 1) {
  // 与原来相同，此处省略，可从原文件复制（未改动）
}