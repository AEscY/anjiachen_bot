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
    'raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  const signed = await crypto.subtle.sign('HMAC', key, enc.encode(msg));
  return btoa(String.fromCharCode(...new Uint8Array(signed)));
}

// ---- OKX 请求封装（支持虚拟盘/实盘切换） ----
async function okxFetch(env, method, endpoint, params = null, body = null) {
  const ts = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
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

  // ✅ 模拟盘/实盘切换开关
  if (env.OKX_MODE === 'demo') {
    headers['x-simulated-trading'] = '1';
  }

  const url = 'https://www.okx.com' + path;
  const opts = { method, headers };
  if (body) opts.body = bodyStr;

  const res = await fetch(url, opts);
  const data = await res.json();
  if (data.code !== '0') {
    throw new Error(`OKX API Error: ${data.msg} (Code: ${data.code})`);
  }
  return data.data;
}

// ---- Telegram 推送 ----
async function sendTG(env, text) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: env.TELEGRAM_CHAT_ID,
      text: text,
      parse_mode: 'MarkdownV2',
      disable_web_page_preview: true
    })
  });
}

// ---- Telegram 机器人命令监听 (Webhook) ----
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === '/webhook' && request.method === 'POST') {
      const update = await request.json();
      if (update.message && update.message.text && update.message.text.startsWith('/status')) {
        await handleStatusCommand(env, update.message.chat.id);
      }
      return new Response('OK');
    }
    return new Response('Anjiachen Bot is running.');
  },

  // ---- 定时任务触发 ----
  async scheduled(event, env, ctx) {
    try {
      await runStrategies(env);
    } catch (e) {
      await sendTG(env, `⚠️ *策略运行错误*:\n\`\`\`\n${e.message}\n\`\`\``);
    }
  }
};

// ---- Telegram /status 命令处理 ----
async function handleStatusCommand(env, chatId) {
  let msg = "🟢 *Anjiachen Bot 运行状态*\n\n";
  msg += `🕹 *交易模式*: ${env.OKX_MODE === 'demo' ? '模拟盘 (Demo)' : '实盘 (Live)'}\n`;
  
  try {
    // 获取模拟/实盘账户余额
    const balances = await okxFetch(env, 'GET', '/api/v5/account/balance');
    const totalEquity = balances[0]?.totalEq || '0';
    msg += `💰 *账户权益*: ${totalEquity} USDT\n\n`;
    
    // 获取当前持仓
    const positions = await okxFetch(env, 'GET', '/api/v5/account/positions');
    if (positions.length > 0) {
      msg += `📊 *当前持仓*:\n`;
      positions.slice(0, 5).forEach(pos => {
        msg += `- ${pos.instId}: ${pos.pos} (${pos.upl} USDT)\n`;
      });
    } else {
      msg += `📊 *当前持仓*: 无\n`;
    }
  } catch (e) {
    msg += `❌ *获取数据失败*: ${e.message}`;
  }
  
  // 直接推送给对应 chat_id
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      text: msg,
      parse_mode: 'MarkdownV2'
    })
  });
}

// ---- 核心策略逻辑 ----
async function runStrategies(env) {
  const modeStr = env.OKX_MODE === 'demo' ? '【模拟盘】' : '【实盘】';
  
  // === 策略1: CEX-DEX 价差监控 ===
  await checkArbitrage(env);
  
  // === 策略2: 双网格交易 ===
  await runGridTrading(env);
}

async function checkArbitrage(env) {
  // 示例：检查 BTC 价格在 OKX 和 链上/其他CEX 的价差
  // 实际应用中可通过 API 获取其他平台价格进行对比
  const okxBtc = await okxFetch(env, 'GET', '/api/v5/market/ticker', { instId: 'BTC-USDT' });
  const btcPrice = parseFloat(okxBtc[0].last);
  
  // 模拟一个价差逻辑（实际可接入 DEX API 如 Uniswap）
  const dexPrice = btcPrice * 1.0015; // 假设 DEX 价格高出 0.15%
  const diffPercent = ((dexPrice - btcPrice) / btcPrice) * 100;

  if (diffPercent > 0.1) {
    const msg = `🚨 *套利机会发现 ${env.OKX_MODE === 'demo' ? '(模拟)' : ''}*\n\n` +
                `币种: BTC\n` +
                `OKX 价格: ${btcPrice}\n` +
                `DEX 价格: ${dexPrice.toFixed(2)}\n` +
                `价差: ${diffPercent.toFixed(3)}%\n` +
                `建议: 买入OKX / 卖出DEX`;
    await sendTG(env, msg);
  }
}

async function runGridTrading(env) {
  // 示例：双网格逻辑 - 检查资金费率，决定网格偏向
  const fundingRate = await okxFetch(env, 'GET', '/api/v5/public/funding-rate', { instId: 'BTC-USDT-SWAP' });
  const rate = parseFloat(fundingRate[0].fundingRate);
  
  let gridMsg = `📉 *双网格运行中 ${env.OKX_MODE === 'demo' ? '(模拟)' : ''}*\n\n`;
  gridMsg += `BTC 资金费率: ${(rate * 100).toFixed(4)}%\n`;
  
  if (rate > 0.0001) {
    gridMsg += `状态: 费率为正，优先执行*做空网格*收割费率。\n`;
  } else if (rate < -0.0001) {
    gridMsg += `状态: 费率为负，优先执行*做多网格*收割费率。\n`;
  } else {
    gridMsg += `状态: 费率中性，执行*双向中性网格*。\n`;
  }
  
  await sendTG(env, gridMsg);
}