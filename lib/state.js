// KV 状态读写封装

const DEFAULTS = {
  ACTIVE_MODE: 'BOTH',
  GRID_CONFIG: {
    lowerPrice: 85000,
    upperPrice: 95000,
    gridCount: 10,
    perGridAmount: 100,
    instId: 'BTC-USDT',
    running: false
  },
  DIP_CONFIG: {
    targetBuyPrice: 80000,
    targetSellPrice: 90000,
    amount: 500,
    instId: 'BTC-USDT',
    running: false
  },
  DIP_STATE: 'WAITING_BUY',
  GRID_PROFIT: 0,
  DIP_PROFIT: 0,
  GRID_FILLS: '[]',
  DIP_FILLS: '[]',
  GRID_TODAY_PROFIT: 0,
  GRID_TODAY_COUNT: 0,
  DIP_ROUNDS: 0,
  NOTIFICATION_LEVEL: 'ALL',
  USER_SESSION: JSON.stringify({
    current_page: 'main',
    input_mode: null,
    input_target: null,
    previous_page: null
  })
};

export async function getState(env, key) {
  const val = await env.KV.get(key);
  if (val === null) return DEFAULTS[key] !== undefined ? DEFAULTS[key] : null;
  try { return JSON.parse(val); } catch { return val; }
}

export async function setState(env, key, value) {
  await env.KV.put(key, typeof value === 'string' ? value : JSON.stringify(value));
}

export async function getActiveMode(env) {
  return await env.KV.get('ACTIVE_MODE') || 'BOTH';
}

export async function getSession(env) {
  const raw = await env.KV.get('USER_SESSION');
  if (!raw) return { current_page: 'main', input_mode: null, input_target: null, previous_page: null };
  return JSON.parse(raw);
}

export async function setSession(env, updates) {
  const session = await getSession(env);
  const updated = { ...session, ...updates };
  await env.KV.put('USER_SESSION', JSON.stringify(updated));
  return updated;
}