'use strict';

function isMemoryAccount(value) {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
}

function sessionKey(msg) {
  const type = msg.chatType === 'group' ? 'g' : 'u';
  if (msg.platform === 'wechat' && isMemoryAccount(msg.memoryAccount)) {
    return `wechat:a:${msg.memoryAccount}:${type}:${msg.chatId}`;
  }
  return `${msg.platform}:${type}:${msg.chatId}`;
}

module.exports = { isMemoryAccount, sessionKey };
