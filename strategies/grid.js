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
    console.error('[Grid] 获取行情失败:', e);
    return;
  }

  // 2. 检查新成交
  const fillsRes = await okxRequest(env, 'GET', '/api/v5/trade/fills', { instId, limit: '20' });
  const lastFillId = (await env.KV.get('LAST_GRID_FILL_ID')) || '0';
  const newFills = (fillsRes.data || []).filter((f) => f.fillId > lastFillId);

  let todayProfit = (await getState(env, 'GRID_TODAY_PROFIT')) || 0;
  let todayCount = (await getState(env, 'GRID_TODAY_COUNT')) || 0;
  let totalProfit = (await getState(env, 'GRID_PROFIT')) || 0;

  for (const fill of newFills) {
    const fillPrice = parseFloat(fill.fillPx);
    const fillSz = parseFloat(fill.fillSz);
    const fee = Math.abs(parseFloat(fill.fee));

    if (fill.side === 'sell') {
      const profit = fillPrice * fillSz - fee;
      todayProfit = parseFloat((todayProfit + profit).toFixed(4));
      totalProfit = parseFloat((totalProfit + profit).toFixed(4));
      todayCount++;
      if (notifyLevel !== 'SILENT') {
        await sendTelegram(
          env,
          `📊 网格卖出成交\n价格：${fillPrice}\n数量：${fillSz}\n利润：+${profit.toFixed(4)} USDT\n手续费：${fee}`
        );
      }
    } else {
      if (notifyLevel === 'ALL') {
        await sendTelegram(env, `📊 网格买入成交\n价格：${fillPrice}\n数量：${fillSz}`);
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

  // 4. 挂单熔断：如果挂单数超过网格数的80%，则跳过补单，避免超限
  const maxPending = Math.ceil(gridCount * 0.8);
  if (pendingOrders.length > maxPending) {
    console.log(`[Grid] 挂单数 ${pendingOrders.length} 超过阈值 ${maxPending}，跳过本次补单`);
    return;
  }

  // 5. 遍历网格层级，补齐缺失的挂单
  const gridLevels = [];
  for (let i = 0; i <= gridCount; i++) {
    gridLevels.push(parseFloat((lowerPrice + i * gridStep).toFixed(2)));
  }

  for (const level of gridLevels) {
    if (level < currentPrice) {
      const hasBuyOrder = pendingOrders.some((o) => parseFloat(o.px) === level && o.side === 'buy');
      if (!hasBuyOrder) {
        const sz = (perGridAmount / level).toFixed(6);
        const orderRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
          instId,
          tdMode: 'cash',
          side: 'buy',
          ordType: 'limit',
          px: level.toString(),
          sz,
        });
        if (orderRes.code === '0' && notifyLevel === 'ALL') {
          console.log(`[Grid] 买单：${level} | ${sz}`);
        }
      }
    }
    if (level > currentPrice) {
      const hasSellOrder = pendingOrders.some((o) => parseFloat(o.px) === level && o.side === 'sell');
      if (!hasSellOrder) {
        const sz = (perGridAmount / level).toFixed(6);
        const orderRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
          instId,
          tdMode: 'cash',
          side: 'sell',
          ordType: 'limit',
          px: level.toString(),
          sz,
        });
        if (orderRes.code === '0' && notifyLevel === 'ALL') {
          console.log(`[Grid] 卖单：${level} | ${sz}`);
        }
      }
    }
  }
}