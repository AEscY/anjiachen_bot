// 网格策略核心逻辑

import { okxRequest, getTicker } from '../lib/okx.js';
import { sendTelegram } from '../lib/telegram.js';
import { getState, setState, getActiveMode } from '../lib/state.js';

export async function runGrid(env) {
  const mode = await getActiveMode(env);
  if (mode !== 'GRID' && mode !== 'BOTH') return;

  const config = await getState(env, 'GRID_CONFIG');
  if (!config.running) return;

  const { lowerPrice, upperPrice, gridCount, perGridAmount, instId } = config;
  const gridStep = (upperPrice - lowerPrice) / gridCount;
  const notifyLevel = await getState(env, 'NOTIFICATION_LEVEL');

  // 1. 获取当前价格
  let currentPrice;
  try {
    currentPrice = await getTicker(instId);
  } catch (e) {
    console.error('获取行情失败:', e);
    return;
  }

  // 2. 检查新成交
  const fillsRes = await okxRequest(env, 'GET', '/api/v5/trade/fills', { instId, limit: '20' });
  const lastFillId = await env.KV.get('LAST_GRID_FILL_ID') || '0';
  const newFills = (fillsRes.data || []).filter(f => f.fillId > lastFillId);

  let todayProfit = await getState(env, 'GRID_TODAY_PROFIT') || 0;
  let todayCount = await getState(env, 'GRID_TODAY_COUNT') || 0;
  let totalProfit = await getState(env, 'GRID_PROFIT') || 0;

  for (const fill of newFills) {
    const fillPrice = parseFloat(fill.fillPx);
    const fillSz = parseFloat(fill.fillSz);
    const fee = Math.abs(parseFloat(fill.fee));

    if (fill.side === 'sell') {
      const profit = (fillPrice * fillSz - fee).toFixed(4);
      todayProfit = parseFloat((todayProfit + parseFloat(profit)).toFixed(4));
      totalProfit = parseFloat((totalProfit + parseFloat(profit)).toFixed(4));
      todayCount++;

      if (notifyLevel !== 'SILENT') {
        await sendTelegram(env,
`📊 网格卖出成交
价格：${fillPrice}
数量：${fillSz}
利润：+${profit} USDT
手续费：${fee}`);
      }
    } else {
      if (notifyLevel === 'ALL') {
        await sendTelegram(env,
`📊 网格买入成交
价格：${fillPrice}
数量：${fillSz}`);
      }
    }
    await env.KV.put('LAST_GRID_FILL_ID', fill.fillId);
  }

  await setState(env, 'GRID_TODAY_PROFIT', todayProfit);
  await setState(env, 'GRID_TODAY_COUNT', todayCount);
  await setState(env, 'GRID_PROFIT', totalProfit);

  // 3. 查询当前挂单
  const pendingRes = await okxRequest(env, 'GET', '/api/v5/trade/orders-pending', { instId });
  const pendingOrders = pendingRes.data || [];

  // 4. 遍历网格层级，补齐缺失的挂单
  const gridLevels = [];
  for (let i = 0; i <= gridCount; i++) {
    gridLevels.push(parseFloat((lowerPrice + i * gridStep).toFixed(2)));
  }

  for (const level of gridLevels) {
    if (level < currentPrice) {
      const hasBuyOrder = pendingOrders.some(o => parseFloat(o.px) === level && o.side === 'buy');
      if (!hasBuyOrder) {
        const sz = (perGridAmount / level).toFixed(6);
        const orderRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
          instId, tdMode: 'cash', side: 'buy', ordType: 'limit', px: level.toString(), sz
        });
        if (orderRes.code === '0' && notifyLevel === 'ALL') {
          console.log(`网格买单：${level} | ${sz}`);
        }
      }
    }
    if (level > currentPrice) {
      const hasSellOrder = pendingOrders.some(o => parseFloat(o.px) === level && o.side === 'sell');
      if (!hasSellOrder) {
        const sz = (perGridAmount / level).toFixed(6);
        const orderRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
          instId, tdMode: 'cash', side: 'sell', ordType: 'limit', px: level.toString(), sz
        });
        if (orderRes.code === '0' && notifyLevel === 'ALL') {
          console.log(`网格卖单：${level} | ${sz}`);
        }
      }
    }
  }
}