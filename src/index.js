// ============================================
//  Anjiachen Bot — OKX 双策略自动化机器人
//  策略1: CEX-DEX 套利监控
//  策略2: 双网格交易
// ============================================

// ---- OKX API 签名 ----
async function signOKX(secret, timestamp, method, path, body = '') {
  const msg = timestamp + method.toUpperCase() + path + body;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false, ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(msg));
  return btoa(String.fromCharCode(...new Uint8Array(sig)));
}

// ---- OKX 请求封装 ----
async function okxFetch(env, method, endpoint, params = null, body = null) {
  const ts = new Date().toISOString();
  const qs = params ? '?' + new URLSearchParams(params).toString() : '';
  const path = endpoint + qs;
  const bodyStr = body ? JSON.stringify(body) : '';
  const sign = await signOKX(env.OKX_SECRET, ts, method, path, bodyStr);

  const headers = {
    'OK-ACCESS-KEY': env.OKX_API_KEY,
    'OK-ACCESS-SIGN': sign,
    'OK-ACCESS-TIMESTAMP': ts,
    'OK-ACCESS-PASSPHRASE': env.OKX_PASSPHRASE,
    'Content-Type': 'application/json'
  };

  const url = 'https://www.okx.com' + path;
  const opts = { method, headers };
  if (body) opts.body = bodyStr;

  const res = await fetch(url, opts);
  const data = await res.json();
  if (data.code !== '0') throw new Error(`OKX Error: ${data.msg}`);
  return data.data;
}

// ---- Telegram 通知 ----
async function sendTG(env, text) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: env.TELEGRAM_CHAT_ID,
      text: text,
      parse_mode: 'Markdown'
    })
  });
}

// ---- 获取 OKX 现货价格 ----
async function getOKXPrice(ticker) {
  const data = await okxFetch(null, 'GET', '/api/v5/market/ticker', { instId: ticker });
  return parseFloat(data[0].last);
}

// ---- 获取 OKX 账户余额 ----
async function getBalance(env, ccy) {
  const data = await okxFetch(env, 'GET', '/api/v5/account/balance', { ccy });
  return data[0]?.details?.[0]?.availBal ? parseFloat(data[0].details[0].availBal) : 0;
}

// ---- 策略1: CEX-DEX 套利监控 ----
async function runArbitrage(env) {
  const pairs = env.ARBITRAGE_PAIRS ? env.ARBITRAGE_PAIRS.split(',') : ['BTC-USDT', 'ETH-USDT'];
  const threshold = parseFloat(env.ARBITRAGE_THRESHOLD || '0.02'); // 2% 价差

  for (const pair of pairs) {
    try {
      const cexPrice = await getOKXPrice(pair);
      // DEX 价格通过 Chainlink/Uniswap 获取（简化版：用 OKX 作为基准）
      // 实际项目中替换为链上价格源
      const dexPrice = cexPrice * (1 + (Math.random() - 0.5) * 0.04); // 模拟

      const diff = Math.abs(cexPrice - dexPrice) / cexPrice;

      if (diff > threshold) {
        const direction = cexPrice > dexPrice ? 'CEX→DEX' : 'DEX→CEX';
        const msg = `🔔 套利机会！\n\n` +
          `交易对: ${pair}\n` +
          `方向: ${direction}\n` +
          `CEX价格: ${cexPrice.toFixed(2)}\n` +
          `DEX价格: ${dexPrice.toFixed(2)}\n` +
          `价差: ${(diff * 100).toFixed(3)}%\n` +
          `时间: ${new Date().toISOString()}`;
        await sendTG(env, msg);
      }
    } catch (e) {
      console.error(`Arbitrage error for ${pair}:`, e.message);
    }
  }
}

// ---- 策略2: 双网格交易 ----
async function runGrid(env) {
  const gridConfig = {
    instId: env.GRID_INST_ID || 'BTC-USDT',
    lower: parseFloat(env.GRID_LOWER || '50000'),
    upper: parseFloat(env.GRID_UPPER || '75000'),
    grids: parseInt(env.GRID_GRIDS || '10'),
    amount: parseFloat(env.GRID_AMOUNT || '0.001')
  };

  try {
    const price = await getOKXPrice(gridConfig.instId);
    const step = (gridConfig.upper - gridConfig.lower) / gridConfig.grids;

    let action = null;
    if (price <= gridConfig.lower + step) {
      action = `买入 ${gridConfig.amount} ${gridConfig.instId.split('-')[0]} @ ${price.toFixed(2)}`;
    } else if (price >= gridConfig.upper - step) {
      action = `卖出 ${gridConfig.amount} ${gridConfig.instId.split('-')[0]} @ ${price.toFixed(2)}`;
    }

    // 读取上次操作时间，避免频繁交易
    const lastTrade = await env.KV.get(`last_trade_${gridConfig.instId}`);
    const now = Date.now();
    const minInterval = 30 * 60 * 1000; // 30分钟冷却

    if (action && (!lastTrade || now - parseInt(lastTrade) > minInterval)) {
      await env.KV.put(`last_trade_${gridConfig.instId}`, now.toString());

      const msg = `📊 网格交易信号\n\n` +
        `交易对: ${gridConfig.instId}\n` +
        `当前价: ${price.toFixed(2)}\n` +
        `操作: ${action}\n` +
        `网格区间: ${gridConfig.lower} - ${gridConfig.upper}\n` +
        `网格数: ${gridConfig.grids}\n` +
        `时间: ${new Date().toISOString()}`;
      await sendTG(env, msg);
    }

    console.log(`Grid: ${gridConfig.instId} price=${price.toFixed(2)}, action=${action || 'none'}`);
  } catch (e) {
    console.error(`Grid error:`, e.message);
  }
}

// ---- 主入口：Cron 触发器 ----
export default {
  async scheduled(event, env, ctx) {
    console.log('⏰ Bot 启动:', new Date().toISOString());

    try {
      await runArbitrage(env);
      await runGrid(env);
      console.log('✅ 本轮执行完成');
    } catch (e) {
      await sendTG(env, `❌ Bot 运行错误:\n${e.message}`);
    }
  }
};