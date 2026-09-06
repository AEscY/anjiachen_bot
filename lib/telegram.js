// Telegram Bot API 封装

export async function sendTelegram(env, text, keyboard = null) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  const body = {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: 'HTML'
  };
  if (keyboard) {
    body.reply_markup = { inline_keyboard: keyboard };
  }
  await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

export async function editTelegram(env, messageId, text, keyboard = null) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/editMessageText`;
  const body = {
    chat_id: env.TELEGRAM_CHAT_ID,
    message_id: messageId,
    text,
    parse_mode: 'HTML'
  };
  if (keyboard) {
    body.reply_markup = { inline_keyboard: keyboard };
  }
  await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

export async function answerCallback(env, callbackQueryId, text = '') {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`;
  await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ callback_query_id: callbackQueryId, text })
  });
}