// 主入口：消息路由 + Cron定时调度
import { routeUpdate } from './lib/router.js';
import { runGrid } from './strategies/grid.js';
import { runDipSell } from './strategies/dip-sell.js';
import { sendTelegram } from './lib/telegram.js';

export default {
  // 定时触发：每5分钟执行一次
  async scheduled(event, env, ctx) {
    try {
      const now = new Date();
      const minute = now.getMinutes();

      // 交替执行：0-4分跑网格，5-9分跑低吸高卖
      if (minute % 10 < 5) {
        await runGrid(env);
      } else {
        await runDipSell(env);
      }
    } catch (error) {
      console.error('[Scheduled] 策略执行异常:', error);
      try {
        await sendTelegram(env, `❌ 策略执行异常\n${error.message}`);
      } catch (e) {}
    }
  },

  // HTTP请求处理：接收Telegram Webhook
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);

      if (url.pathname === '/telegram' && request.method === 'POST') {
        const update = await request.json();
        console.log('[Webhook] 收到更新:', JSON.stringify(update).slice(0, 200));
        ctx.waitUntil(routeUpdate(env, update));
        return new Response('ok', { status: 200 });
      }

      if (url.pathname === '/health') {
        return new Response(JSON.stringify({ status: 'ok', time: new Date().toISOString() }), {
          headers: { 'Content-Type': 'application/json' },
        });
      }

      return new Response('OKX Bot is running', { status: 200 });
    } catch (error) {
      console.error('[Fetch] 错误:', error);
      return new Response('error: ' + error.message, { status: 500 });
    }
  },
};