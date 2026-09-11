// 低吸高卖策略核心逻辑（基于订单ID状态轮询）
import { okxRequest, getTicker } from '../lib/okx.js';
import { sendTelegram } from '../lib/telegram.js';
import { getState, setState, getActiveMode } from '../lib/state.js';

export async function runDipSell(env) {
  const mode = await getActiveMode(env);
  if (mode !== 'DIP_SELL' && mode !== 'BOTH') return;

  const config = await getState(env, 'DIP_CONFIG');
  if (!config.running) return;

  const { targetBuyPrice, targetSellPrice, amount, instId } = config;
  const status = (await getState(env, 'DIP_STATE')) || 'WAITING_BUY';
  const notifyLevel = await getState(env, 'NOTIFICATION_LEVEL');

  let currentPrice;
  try {
    currentPrice = await getTicker(instId);
  } catch (e) {
    console.error('[Dip] 获取行情失败:', e);
    return;
  }

  // 状态1：等待低吸
  if (status === 'WAITING_BUY') {
    if (currentPrice <= targetBuyPrice) {
      const sz = (amount / targetBuyPrice).toFixed(6);
      const orderRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
        instId,
        tdMode: 'cash',
        side: 'buy',
        ordType: 'limit',
        px: targetBuyPrice.toString(),
        sz,
      });
      if (orderRes.code === '0') {
        const ordId = orderRes.data[0].ordId;
        await setState(env, 'DIP_BUY_ORD_ID', ordId);
        await setState(env, 'DIP_STATE', 'BUYING');
        if (notifyLevel !== 'SILENT') {
          await sendTelegram(
            env,
            `🎯 低吸触发！\n当前价：${currentPrice}\n目标买价：${targetBuyPrice}\n挂单数量：${sz}\n订单ID: ${ordId}\n等待成交中...`
          );
        }
      } else {
        if (notifyLevel !== 'SILENT') {
          await sendTelegram(env, `❌ 低吸挂单失败\n错误码：${orderRes.code}\n信息：${orderRes.msg}`);
        }
      }
    }
  }

  // 状态2：买单已挂，根据订单ID查询状态
  else if (status === 'BUYING') {
    const buyOrdId = await getState(env, 'DIP_BUY_ORD_ID');
    if (!buyOrdId) {
      // 如果没有订单ID，重置状态
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      return;
    }
    const orderInfo = await okxRequest(env, 'GET', '/api/v5/trade/order', { ordId: buyOrdId, instId });
    const order = orderInfo.data?.[0];
    if (!order) {
      console.warn('[Dip] 未找到订单，重置状态');
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      return;
    }

    if (order.state === 'filled') {
      // 买单成交，挂卖出单
      const fillSz = order.sz;
      const sellRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
        instId,
        tdMode: 'cash',
        side: 'sell',
        ordType: 'limit',
        px: targetSellPrice.toString(),
        sz: fillSz,
      });
      if (sellRes.code === '0') {
        const sellOrdId = sellRes.data[0].ordId;
        await setState(env, 'DIP_SELL_ORD_ID', sellOrdId);
        await setState(env, 'DIP_STATE', 'WAITING_SELL');
        const expectedProfit = ((targetSellPrice - targetBuyPrice) * parseFloat(fillSz)).toFixed(2);
        if (notifyLevel !== 'SILENT') {
          await sendTelegram(
            env,
            `🎯 低吸成交！已自动挂高卖单\n买入价：${targetBuyPrice}\n卖出价：${targetSellPrice}\n数量：${fillSz}\n预期利润：+${expectedProfit} USDT`
          );
        }
      } else {
        if (notifyLevel !== 'SILENT') {
          await sendTelegram(env, `❌ 挂卖单失败\n错误码：${sellRes.code}\n信息：${sellRes.msg}`);
        }
      }
    } else if (order.state === 'canceled' || order.state === 'expired') {
      // 订单取消或过期，重置状态
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      await setState(env, 'DIP_BUY_ORD_ID', null);
      if (notifyLevel !== 'SILENT') {
        await sendTelegram(env, '⏹ 低吸买单已取消或过期，进入等待下一轮');
      }
    }
    // 其他状态（pending）则等待
  }

  // 状态3：等待高卖成交
  else if (status === 'WAITING_SELL') {
    const sellOrdId = await getState(env, 'DIP_SELL_ORD_ID');
    if (!sellOrdId) {
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      return;
    }
    const orderInfo = await okxRequest(env, 'GET', '/api/v5/trade/order', { ordId: sellOrdId, instId });
    const order = orderInfo.data?.[0];
    if (!order) {
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      return;
    }

    if (order.state === 'filled') {
      // 卖单成交，计算利润
      const fillSz = order.sz;
      const profit = (targetSellPrice - targetBuyPrice) * parseFloat(fillSz);
      let totalProfit = (await getState(env, 'DIP_PROFIT')) || 0;
      totalProfit = parseFloat((totalProfit + profit).toFixed(2));
      let rounds = (await getState(env, 'DIP_ROUNDS')) || 0;
      rounds++;

      await setState(env, 'DIP_PROFIT', totalProfit);
      await setState(env, 'DIP_ROUNDS', rounds);
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      await setState(env, 'DIP_BUY_ORD_ID', null);
      await setState(env, 'DIP_SELL_ORD_ID', null);

      // 记录历史
      const history = JSON.parse((await env.KV.get('DIP_FILLS')) || '[]');
      history.unshift({
        round: rounds,
        buyPrice: targetBuyPrice,
        sellPrice: targetSellPrice,
        profit: profit,
        time: new Date().toISOString(),
      });
      await env.KV.put('DIP_FILLS', JSON.stringify(history.slice(0, 50)));

      if (notifyLevel !== 'SILENT') {
        await sendTelegram(
          env,
          `🎉 低吸高卖第${rounds}轮完成！\n买入价：${targetBuyPrice}\n卖出价：${targetSellPrice}\n本轮利润：+${profit.toFixed(2)} USDT\n累计收益：+${totalProfit} USDT\n\n自动进入下一轮等待低吸...`
        );
      }
    } else if (order.state === 'canceled' || order.state === 'expired') {
      // 卖单取消，重置状态
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      await setState(env, 'DIP_SELL_ORD_ID', null);
      if (notifyLevel !== 'SILENT') {
        await sendTelegram(env, '⏹ 高卖订单已取消或过期，重置为等待低吸');
      }
    }
  }
}