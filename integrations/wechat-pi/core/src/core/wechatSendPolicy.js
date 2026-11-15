'use strict';

const { nameMatches } = require('./text');

function configuredWhitelist(config = {}) {
  const raw = config.care?.whitelist;
  const list = Array.isArray(raw) ? raw : (raw ? String(raw).split(/[\n,;，；\s]+/) : []);
  return [...new Set(list.map((value) => String(value || '').trim()).filter(Boolean))];
}

/** null 表示可发。选择器存会话 ID，群名只用来兼容旧版文字禁言。 */
function sendDenial(config = {}, { id, names = [], isGroup = false, platform = 'wechat' }) {
  const key = String(id || '').trim().toLowerCase();
  const wechat = platform === 'wechat';
  const blocked = config.group?.mutedGroups;
  if ((wechat || isGroup) && Array.isArray(blocked) && blocked.some((value) => {
    const entry = String(value || '').trim().toLowerCase();
    return entry === '*' || (key && key === entry) || (isGroup && names.some((name) => nameMatches(name, entry)));
  })) return isGroup ? 'muted_group' : 'muted_chat';
  if (wechat && (!key || key === 'unknown') && (blocked?.length || config.wechat?.whitelistOnly === true)) return 'unresolved_chat';
  if (wechat && config.wechat?.whitelistOnly === true &&
      !configuredWhitelist(config).some((entry) => entry.toLowerCase() === key)) return 'not_whitelisted';
  return null;
}

module.exports = { configuredWhitelist, sendDenial };
