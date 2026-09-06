// 消息路由：分发文本指令和按钮回调

import { handleMenuPage } from '../ui/main-menu.js';
import { handleGridPanel, handleGridParams, handleGridFills } from '../ui/grid-panel.js';
import { handleDipPanel, handleDipParams, handleDipHistory } from '../ui/dip-sell-panel.js';
import { handleSettings, handleSwitchMode, handleNotifySettings, handleHelp } from '../ui/settings-panel.js';
import { handleProfitPanel } from '../ui/profit-panel.js';
import { handleAction } from './actions.js';
import { getSession, setSession, getState, setState } from './state.js';
import { sendTelegram } from './telegram.js';

export async function routeUpdate(env, update) {
  // 处理普通文本消息
  if (update.message) {
    const text = (update.message.text || '').trim();
    const session = await getSession(env);

    // 如果处于等待输入状态，拦截文本作为参数值
    if (session.input_mode === 'param_input') {
      return await handleParamInput(env, text, session);
    }

    // 文本指令路由
    if (text === '/start' || text === '/menu') {
      return await handleMenuPage(env);
    }
    if (text === '/status') {
      return await handleQuickStatus(env);
    }
    if (text.startsWith('/switch')) {
      const mode = text.split(' ')[1];
      if (['grid', 'dip', 'both'].includes(mode)) {
        return await handleAction(env, 'switch_' + mode);
      }
    }
    if (text === '/grid_start') return await handleAction(env, 'grid_start');
    if (text === '/grid_stop') return await handleAction(env, 'grid_stop');
    if (text === '/dip_start') return await handleAction(env, 'dip_start');
    if (text === '/dip_stop') return await handleAction(env, 'dip_stop');

    // 非指令文本，默认打开主菜单
    return await handleMenuPage(env);
  }

  // 处理按钮回调
  if (update.callback_query) {
    const callbackData = update.callback_query.data;

    // noop 按钮（分页显示用）
    if (callbackData === 'noop') return;

    const [type, ...rest] = callbackData.split(':');
    const payload = rest.join(':');

    if (type === 'page') return await routePage(env, payload);
    if (type === 'action') return await handleAction(env, payload);
    if (type === 'param') return await handleParamEntry(env, payload);
    if (type === 'confirm') return await handleAction(env, 'confirm_' + payload);
    if (type === 'cancel') return await handleCancel(env, payload);
    if (type === 'nav') return await handleNav(env, payload);
  }
}

async function routePage(env, page) {
  switch (page) {
    case 'main': return await handleMenuPage(env);
    case 'grid_panel': return await handleGridPanel(env);
    case 'grid_params': return await handleGridParams(env);
    case 'grid_fills': return await handleGridFills(env, 1);
    case 'dip_panel': return await handleDipPanel(env);
    case 'dip_params': return await handleDipParams(env);
    case 'dip_history': return await handleDipHistory(env, 1);
    case 'settings': return await handleSettings(env);
    case 'switch_mode': return await handleSwitchMode(env);
    case 'notify_settings': return await handleNotifySettings(env);
    case 'help': return await handleHelp(env);
    case 'profit': return await handleProfitPanel(env);
  }
}

async function handleParamInput(env, text, session) {
  const target = session.input_target;
  const value = parseFloat(text);
  if (isNaN(value)) {
    await sendTelegram(env, '❌ 请输入有效的数字');
    return;
  }
  if (target.startsWith('grid_')) {
    const config = await getState(env, 'GRID_CONFIG');
    const keyMap = { grid_lower: 'lowerPrice', grid_upper: 'upperPrice', grid_count: 'gridCount', grid_amount: 'perGridAmount' };
    if (keyMap[target]) {
      config[keyMap[target]] = value;
      await setState(env, 'GRID_CONFIG', config);
      await sendTelegram(env, `✅ 网格参数【${target}】已更新为 ${value}，重启策略后生效`);
    }
  } else if (target.startsWith('dip_')) {
    const config = await getState(env, 'DIP_CONFIG');
    const keyMap = { dip_buy: 'targetBuyPrice', dip_sell: 'targetSellPrice', dip_amount: 'amount' };
    if (keyMap[target]) {
      config[keyMap[target]] = value;
      await setState(env, 'DIP_CONFIG', config);
      await sendTelegram(env, `✅ 低吸高卖参数【${target}】已更新为 ${value}，重启策略后生效`);
    }
  }
  await setSession(env, { input_mode: null, input_target: null });
}

async function handleParamEntry(env, payload) {
  const labelMap = {
    grid_lower: '价格下限', grid_upper: '价格上限',
    grid_count: '网格数量', grid_amount: '每格金额',
    dip_buy: '目标买价', dip_sell: '目标卖价', dip_amount: '投入金额'
  };
  await setSession(env, { input_mode: 'param_input', input_target: payload });
  await sendTelegram(env, `✏️ 请输入新的【${labelMap[payload] || payload}】\n💡 直接发送数字即可\n\n例：85000`);
}

async function handleCancel(env, payload) {
  if (payload === 'param_input') {
    await setSession(env, { input_mode: null, input_target: null });
    await sendTelegram(env, '已取消修改');
  }
}

async function handleNav(env, payload) {
  const parts = payload.split(':');
  const page = parts[0];
  const pageNum = parseInt(parts[1]) || 1;
  if (page === 'grid_fills') return await handleGridFills(env, pageNum);
  if (page === 'dip_history') return await handleDipHistory(env, pageNum);
}

async function handleQuickStatus(env) {
  const mode = await getActiveMode(env);
  const gridConfig = await getState(env, 'GRID_CONFIG');
  const dipConfig = await getState(env, 'DIP_CONFIG');
  const gridProfit = await getState(env, 'GRID_PROFIT') || 0;
  const dipProfit = await getState(env, 'DIP_PROFIT') || 0;
  const modeLabel = { GRID: '仅网格', DIP_SELL: '仅低吸高卖', BOTH: '双模式并行' };
  const msg =
`📊 策略状态摘要

模式：${modeLabel[mode] || mode}
网格：${gridConfig.running ? '🟢 运行中' : '⏸ 已暂停'} | 累计 +${gridProfit} USDT
低吸高卖：${dipConfig.running ? '🟢 运行中' : '⏸ 已暂停'} | 累计 +${dipProfit} USDT`;
  await sendTelegram(env, msg);
}

async function getActiveMode(env) {
  return await env.KV.get('ACTIVE_MODE') || 'BOTH';
}