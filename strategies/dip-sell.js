// 低吸高卖策略核心逻辑

import { okxRequest, getTicker } from '../lib/okx.js';
import { sendTelegram } from '../lib/telegram.js';
import { getState, setState, getActiveMode } from '../lib/state.js';

export async function runDipSell(env) {
  const mode = await getActiveMode(env);
  if (mode !== 'DIP_SELL' && mode !== 'BOTH') return;

  const config = await getState(env, 'DIP_CONFIG');
  if (!config.running) return;

  const { targetBuyPrice, targetSellPrice, amount, instId } = config;
  const status = await getState(env, 'DIP_STATE') || 'WAITING_BUY';
  const notifyLevel = await getState(env, 'NOTIFICATION_LEVEL');

  let currentPrice;
  try {
    currentPrice = await getTicker(instId);
  } catch (e) {
    console.error('获取行情失败:', e);
    return;
  }

  // 状态1：等待低吸
  if (status === 'WAITING_BUY') {
    if (currentPrice <= targetBuyPrice) {
      const sz = (amount / targetBuyPrice).toFixed(6);
      const orderRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
        instId, tdMode: 'cash', side: 'buy', ordType: 'limit',
        px: targetBuyPrice.toString(), sz
      });
      if (orderRes.code === '0') {
        await setState(env, 'DIP_STATE', 'BUYING');
        if (notifyLevel !== 'SILENT') {
          await sendTelegram(env,
`🎯 低吸触发！
当前价：${currentPrice}
目标买价：${targetBuyPrice}
挂单数量：${sz}
等待成交中...`);
        }
      } else {
        if (notifyLevel !== 'SILENT') {
          await sendTelegram(env, `❌ 低吸挂单失败\n错误码：${orderRes.code}\n信息：${orderRes.msg}`);
        }
      }
    }
  }

  // 状态2：买单已挂，检查成交
  else if (status === 'BUYING') {
    const fillsRes = await okxRequest(env, 'GET', '/api/v5/trade/fills', { instId, limit: '5' });
    const buyFill = (fillsRes.data || []).find(
      f => f.side === 'buy' && Math.abs(parseFloat(f.fillPx) - targetBuyPrice) < 1
    );
    if (buyFill) {
      const sz = buyFill.fillSz;
      const orderRes = await okxRequest(env, 'POST', '/api/v5/trade/order', null, {
        instId, tdMode: 'cash', side: 'sell', ordType: 'limit',
        px: targetSellPrice.toString(), sz
      });
      if (orderRes.code === '0') {
        await setState(env, 'DIP_STATE', 'WAITING_SELL');
        const expectedProfit = ((targetSellPrice - targetBuyPrice) * parseFloat(sz)).toFixed(2);
        if (notifyLevel !== 'SILENT') {
          await sendTelegram(env,
`🎯 低吸成交！已自动挂高卖单
买入价：${targetBuyPrice}
卖出价：${targetSellPrice}
数量：${sz}
预期利润：+${expectedProfit} USDT`);
        }
      }
    }
  }

  // 状态3：等待高卖成交
  else if (status === 'WAITING_SELL') {
    const fillsRes = await okxRequest(env, 'GET', '/api/v5/trade/fills', { instId, limit: '5' });
    const sellFill = (fillsRes.data || []).find(
      f => f.side === 'sell' && Math.abs(parseFloat(f.fillPx) - targetSellPrice) < 1
    );
    if (sellFill) {
      const profit = ((targetSellPrice - targetBuyPrice) * parseFloat(sellFill.fillSz)).toFixed(2);
      let totalProfit = await getState(env, 'DIP_PROFIT') || 0;
      totalProfit = parseFloat((totalProfit + parseFloat(profit)).toFixed(2));
      let rounds = await getState(env, 'DIP_ROUNDS') || 0;
      rounds++;

      await setState(env, 'DIP_PROFIT', totalProfit);
      await setState(env, 'DIP_ROUNDS', rounds);
      await setState(env, 'DIP_STATE', 'WAITING_BUY');

      // 记录历史
      const history = JSON.parse(await env.KV.get('DIP_FILLS') || '[]');
      history.unshift({
        round: rounds,
        buyPrice: targetBuyPrice,
        sellPrice: targetSellPrice,
        profit: parseFloat(profit),
        time: new Date().toISOString()
      });
      await env.KV.put('DIP_FILLS', JSON.stringify(history.slice(0, 50)));

      if (notifyLevel !== 'SILENT') {
        await sendTelegram(env,
`🎉 低吸高卖第${rounds}轮完成！
买入价：${targetBuyPrice}
卖出价：${targetSellPrice}
本轮利润：+${profit} USDT
累计收益：+${totalProfit} USDT

自动进入下一轮等待低吸...`);
      }
    }
  }
}