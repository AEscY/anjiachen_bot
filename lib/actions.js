// 所有按钮操作的执行逻辑
import { getState, setState, getActiveMode } from './state.js';
import { sendTelegram } from './telegram.js';
import { okxRequest } from './okx.js';

export async function handleAction(env, action) {
  switch (action) {
    // === 网格策略操作 ===
    case 'grid_start': {
      const config = await getState(env, 'GRID_CONFIG');
      config.running = true;
      await setState(env, 'GRID_CONFIG', config);
      await sendTelegram(env, '✅ 网格策略已启动');
      break;
    }
    case 'grid_stop': {
      const config = await getState(env, 'GRID_CONFIG');
      config.running = false;
      await setState(env, 'GRID_CONFIG', config);
      await sendTelegram(env, '⏸ 网格策略已暂停');
      break;
    }
    case 'grid_rebalance': {
      await sendTelegram(env, '🔄 正在撤销所有网格挂单...');
      try {
        const config = await getState(env, 'GRID_CONFIG');
        const pendingRes = await okxRequest(env, 'GET', '/api/v5/trade/orders-pending', {
          instId: config.instId,
        });
        let count = 0;
        for (const order of pendingRes.data || []) {
          await okxRequest(env, 'POST', '/api/v5/trade/cancel-order', null, {
            instId: order.instId,
            ordId: order.ordId,
          });
          count++;
        }
        await sendTelegram(env, `✅ 已撤销 ${count} 笔挂单，下次定时触发时将重新挂单`);
      } catch (e) {
        await sendTelegram(env, `❌ 重新挂单失败：${e.message}`);
      }
      break;
    }

    // === 低吸高卖操作 ===
    case 'dip_start': {
      const config = await getState(env, 'DIP_CONFIG');
      config.running = true;
      await setState(env, 'DIP_CONFIG', config);
      await sendTelegram(env, '✅ 低吸高卖策略已启动');
      break;
    }
    case 'dip_stop': {
      const config = await getState(env, 'DIP_CONFIG');
      config.running = false;
      await setState(env, 'DIP_CONFIG', config);
      await sendTelegram(env, '⏸ 低吸高卖策略已暂停');
      break;
    }
    case 'dip_reset': {
      await setState(env, 'DIP_STATE', 'WAITING_BUY');
      await setState(env, 'DIP_BUY_ORD_ID', null); // 清除订单ID
      await sendTelegram(env, '🔄 低吸高卖已重置，重新开始等待低吸');
      break;
    }

    // === 模式切换 ===
    case 'switch_grid': {
      await setState(env, 'ACTIVE_MODE', 'GRID');
      await sendTelegram(env, '✅ 已切换到【仅网格模式】\n低吸高卖策略已暂停');
      break;
    }
    case 'switch_dip': {
      await setState(env, 'ACTIVE_MODE', 'DIP_SELL');
      await sendTelegram(env, '✅ 已切换到【仅低吸高卖模式】\n网格策略已暂停');
      break;
    }
    case 'switch_both': {
      await setState(env, 'ACTIVE_MODE', 'BOTH');
      await sendTelegram(env, '✅ 已切换到【双模式并行】\n两个策略同时运行');
      break;
    }

    // === 通知设置 ===
    case 'notify_silent': {
      await setState(env, 'NOTIFICATION_LEVEL', 'SILENT');
      await sendTelegram(env, '🔕 已切换为静默模式，仅推送异常告警');
      break;
    }
    case 'notify_trade': {
      await setState(env, 'NOTIFICATION_LEVEL', 'TRADE');
      await sendTelegram(env, '📬 已切换为仅成交通知模式');
      break;
    }
    case 'notify_all': {
      await setState(env, 'NOTIFICATION_LEVEL', 'ALL');
      await sendTelegram(env, '📢 已切换为全部通知模式');
      break;
    }

    // === API 安全检查 ===
    case 'api_check': {
      try {
        const res = await okxRequest(env, 'GET', '/api/v5/account/balance');
        if (res.code === '0') {
          const totalBal = parseFloat(res.data[0].totalEq).toFixed(2);
          await sendTelegram(
            env,
            `🛡️ API安全检查通过\n\n账户权益：${totalBal} USDT\nAPI权限：正常\n连接状态：✅ 正常`
          );
        } else {
          await sendTelegram(
            env,
            `🛡️ API安全检查\n\n状态：❌ 异常\n错误码：${res.code}\n信息：${res.msg}`
          );
        }
      } catch (e) {
        await sendTelegram(env, `🛡️ API安全检查\n\n状态：❌ 连接失败\n原因：${e.message}`);
      }
      break;
    }
    default:
      console.warn('[Action] 未知操作:', action);
  }
}