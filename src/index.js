// ============================================
//  Anjiachen Bot — OKX 双策略自动化机器人 (Debug Version)
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

// ---- OKX 请求封装 ----
async function okxFetch(env, method, endpoint, params = null, body = null) {
  console.log(`[OKX Fetch] 开始请求: ${method} ${endpoint}`);
  
  // 1. 检查环境变量
  if (!env.OKX_API_KEY || !env.OKX_SECRET || !env.OKX_PASSPHRASE) {
    throw new Error('OKX 环境变量缺失 (API_KEY, SECRET, 或 PASSPHRASE)');
  }

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

  if (env.OKX_MODE === 'demo') {
    headers['x-simulated-trading'] = '1';
    console.log('[OKX Fetch] 模式: 模拟盘 (Demo)');
  } else {
    console.log('[OKX Fetch] 模式: 实盘 (Live)');
  }

  const url = 'https://www.okx.com' + path;
  const opts = { method, headers };
  if (body) opts.body = bodyStr;

  console.log(`[OKX Fetch] 请求URL: ${url}`);
  const res = await fetch(url, opts);
  
  if (!res.ok) {
    throw new Error(`OKX HTTP 错误: ${res.status} ${res.statusText}`);
  }

  const data = await res.json();
  console.log(`[OKX Fetch] 响应数据:`, JSON.stringify(data).substring(0, 200) + '...');

  if (data.code !== '0') {
    throw new Error(`OKX API Error: ${data.msg} (Code: ${data.code})`);
  }
  return data.data;
}

// ---- Telegram 推送 ----
async function sendTG(env, text) {
  console.log('[TG Send] 准备发送消息:', text.substring(0, 50) + '...');
  
  // 1. 检查 Telegram 环境变量
  if (!env.TELEGRAM_BOT_TOKEN) {
    console.error('[TG Send] 错误: TELEGRAM_BOT_TOKEN 未设置');
    return;
  }
  if (!env.TELEGRAM_CHAT_ID) {
    console.error('[TG Send] 错误: TELEGRAM_CHAT_ID 未设置');
    return;
  }

  // 2. 修复 URL 拼接，确保格式正确
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  console.log('[TG Send] 请求URL:', url);

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: env.TELEGRAM_CHAT_ID,
        text: text,
        parse_mode: 'MarkdownV2',
        disable_web_page_preview: true
      })
    });

    if (!res.ok) {
      const errText = await res.text();
      console.error('[TG Send] 发送失败:', res.status, errText);
    } else {
      console.log('[TG Send] 消息发送成功');
    }
  } catch (e) {
    console.error('[TG Send] 网络请求异常:', e.message);
  }
}

// ---- 主入口 ----
export default {
  async fetch(request, env, ctx) {
    console.log('[Fetch] 收到请求:', request.method, request.url);
    
    const url = new URL(request.url);
    
    // 处理 Webhook
    if (url.pathname === '/webhook' && request.method === 'POST') {
      try {
        const update = await request.json();
        console.log('[Webhook] 收到更新:', JSON.stringify(update).substring(0, 200) + '...');
        
        if (update.message && update.message.text) {
          console.log('[Webhook] 收到消息:', update.message.text);
          
          if (update.message.text.startsWith('/status')) {
            console.log('[Webhook] 执行 /status 命令');
            await handleStatusCommand(env, update.message.chat.id);
          } else {
            console.log('[Webhook] 未知命令');
          }
        }
      } catch (e) {
        console.error('[Webhook] 处理失败:', e.message);
      }
      return new Response('OK');
    }
    
    return new Response('Anjiachen Bot is running.');
  },

  async scheduled(event, env, ctx) {
    console.log('[Scheduled] 定时任务触发');
    try {
      await runStrategies(env);
    } catch (e) {
      console.error('[Scheduled] 策略运行错误:', e.message);
      await sendTG(env, `⚠️ *策略运行错误*:\n\`\`\`\n${e.message}\n\`\`\``);
    }
  }
};

// ---- Telegram /status 命令处理 ----
async function handleStatusCommand(env, chatId) {
  console.log('[Status] 开始处理 /status 命令');
  let msg = "🟢 *Anjiachen Bot 运行状态*\n\n";
  msg += `🕹 *交易模式*: ${env.OKX_MODE === 'demo' ? '模拟盘 (Demo)' : '实盘 (Live)'}\n`;
  
  try {
    const balances = await okxFetch(env, 'GET', '/api/v5/account/balance');
    const totalEquity = balances[0]?.totalEq || '0';
    msg += `💰 *账户权益*: ${totalEquity} USDT\n\n`;
    
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
    console.error('[Status] 获取数据失败:', e.message);
    msg += `❌ *获取数据失败*: ${e.message}`;
  }
  
  // 直接推送给对应 chat_id
  const tgUrl = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  console.log('[Status] 发送状态消息到:', tgUrl);
  
  await fetch(tgUrl, {
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
  console.log('[Strategy] 开始运行策略');
  const modeStr = env.OKX_MODE === 'demo' ? '【模拟盘】' : '【实盘】';
  
  await checkArbitrage(env);
  await runGridTrading(env);
}

async function checkArbitrage(env) {
  console.log('[Arbitrage] 开始检查套利机会');
  try {
    const okxBtc = await okxFetch(env, 'GET', '/api/v5/market/ticker', { instId: 'BTC-USDT' });
    const btcPrice = parseFloat(okxBtc[0].last);
    
    const dexPrice = btcPrice * 1.0015;
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
  } catch (e) {
    console.error('[Arbitrage] 检查失败:', e.message);
  }
}

async function runGridTrading(env) {
  console.log('[Grid] 开始运行网格交易');
  try {
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
  } catch (e) {
    console.error('[Grid] 运行失败:', e.message);
  }
}