// OKX API 签名与请求封装
export async function signRequest(secret, timestamp, method, requestPath, body = '') {
  const message = timestamp + method.toUpperCase() + requestPath + body;
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const signature = await crypto.subtle.sign('HMAC', key, encoder.encode(message));
  return btoa(String.fromCharCode(...new Uint8Array(signature)));
}

export async function okxRequest(env, method, endpoint, params = null, body = null) {
  const timestamp = new Date().toISOString();
  const requestPath = endpoint + (params ? '?' + new URLSearchParams(params).toString() : '');
  const bodyStr = body ? JSON.stringify(body) : '';
  const signature = await signRequest(env.OKX_SECRET, timestamp, method, requestPath, bodyStr);

  const headers = {
    'OK-ACCESS-KEY': env.OKX_API_KEY,
    'OK-ACCESS-SIGN': signature,
    'OK-ACCESS-TIMESTAMP': timestamp,
    'OK-ACCESS-PASSPHRASE': env.OKX_PASSPHRASE,
    'Content-Type': 'application/json',
  };

  if (env.OKX_MODE === 'demo') {
    headers['x-simulated-trading'] = '1';
  }

  const url = 'https://www.okx.com' + requestPath;
  const options = { method, headers };
  if (body) options.body = bodyStr;

  const response = await fetch(url, options);
  const data = await response.json();

  if (data.code !== '0') {
    console.error('[OKX] API Error:', JSON.stringify(data));
  }
  return data;
}

export async function getTicker(instId = 'BTC-USDT') {
  const url = `https://www.okx.com/api/v5/market/ticker?instId=${instId}`;
  const res = await fetch(url);
  const data = await res.json();
  return parseFloat(data.data[0].last);
}