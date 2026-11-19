'use strict';

/**
 * 主人身份认证 —— 全项目只在这里判断「这条消息是不是主人发的」。
 *
 * 为什么要单独一个文件：以前消息处理和提示词预览各写了一套规则，两边不一致 ——
 * 预览显示「识别为主人」，微信里却当陌生人，于是在面板里怎么改都像没生效。
 * 现在两边都调 matchMaster()。
 *
 * 判定顺序：
 *   1. senderId 等于 master.wxids 里的某一项 → 是主人（微信内部 id，改不了昵称也冒充不了）
 *   2. 非微信渠道（网页等）没有稳定 id，昵称对上就算
 *   3. 微信里只有当 master.wxids 里没有任何「像 wxid 的值」时，才退回用昵称认
 *      —— 微信里谁都能改昵称，配了真 wxid 还认昵称就等于给所有人开后门
 */

const WXID_RE = /^wxid_[A-Za-z0-9_-]+$/;
// 账号目录名比真 wxid 多 4 位哈希（wxid_xxx_c248），配置里两种写法都可能出现。
const ACCOUNT_HASH_RE = /_[0-9a-f]{4}$/i;

function asList(value) {
  return (Array.isArray(value) ? value : [])
    .map((v) => String(v == null ? '' : v).trim())
    .filter(Boolean);
}

/** 取出主人名单（顺手去掉空行和首尾空格，界面里一行一个） */
function masterLists(config) {
  const master = (config && config.persona && config.persona.master) || {};
  return { names: asList(master.names), wxids: asList(master.wxids) };
}

/** 这串是不是微信内部 id（wxid_xxx）。自己设的微信号（形如 abc12345）和昵称都不算 */
function looksLikeWxid(value) {
  return WXID_RE.test(String(value == null ? '' : value).trim());
}

/** 去掉账号目录名末尾的 4 位哈希，得到真 wxid */
function stripAccountHash(value) {
  return String(value == null ? '' : value).trim().replace(ACCOUNT_HASH_RE, '');
}

/**
 * 两个 id 是不是同一个人。
 * 启动器的账号列表显示的是目录名 `wxid_xxx_c248`，用户复制到主人 wxid 里也常常带哈希；
 * 消息里的 senderId 却是真 wxid `wxid_xxx`。以前按字符串全等比较，
 * 填了目录名就等于「不是主人」，日志里会反复出现「→ 非主人」。
 */
function sameWxid(a, b) {
  const x = stripAccountHash(a).toLowerCase();
  const y = stripAccountHash(b).toLowerCase();
  return Boolean(x) && x === y;
}

/**
 * 判断这条消息的发送者是不是主人。
 * @param {object} config 运行时配置
 * @param {{platform?: string, senderId?: string, senderName?: string}} msg
 * @returns {{
 *   isMaster: boolean, reason: string, names: string[], wxids: string[], hardIds: string[],
 *   nameHit: boolean, nameFallback: boolean, senderId: string, senderName: string, platform: string
 * }}
 * reason: 'wxid' 按 id 认的 / 'nickname' 按昵称认的 / 'name_blocked' 昵称对上了但被 rule 3 挡住 / '' 都不是
 */
function matchMaster(config, msg = {}) {
  const { names, wxids } = masterLists(config);
  const senderId = String((msg && msg.senderId) || '').trim();
  const senderName = String((msg && msg.senderName) || '').trim();
  const platform = String((msg && msg.platform) || '').trim().toLowerCase();

  const idHit = Boolean(senderId) && wxids.some((w) => sameWxid(w, senderId));
  const nameHit = Boolean(senderName) && names.some((n) => n.toLowerCase() === senderName.toLowerCase());
  const hardIds = wxids.filter(looksLikeWxid);
  // 微信里只信 senderId；网页这类渠道没有稳定 id，才允许昵称兜底
  const nameFallback = platform !== 'wechat' || hardIds.length === 0;
  const isMaster = idHit || (nameFallback && nameHit);

  let reason = '';
  if (idHit) reason = 'wxid';
  else if (isMaster) reason = 'nickname';
  else if (nameHit) reason = 'name_blocked';

  return { isMaster, reason, names, wxids, hardIds, nameHit, nameFallback, senderId, senderName, platform };
}

/** 配置明显有问题时返回人话提示（启动器和日志用，不影响判定） */
function masterWarnings(config) {
  const { names, wxids } = masterLists(config);
  const out = [];
  if (!names.length && !wxids.length) {
    out.push('还没设置主人：persona.master.names / wxids 都是空的，谁都不算主人');
    return out;
  }
  const bad = wxids.filter((w) => !looksLikeWxid(w));
  if (bad.length) {
    out.push(
      '「' + bad.join('、') + '」不是 wxid（微信内部 id 形如 wxid_xxxxxxxx，不是你设置的微信号），' +
      '在微信里认不出来'
    );
  }
  if (!wxids.length && names.length) {
    out.push('现在只靠昵称认主人：微信里谁都能改成同名，建议在 master.wxids 里补一个真正的 wxid');
  }
  // 账号列表显示的是目录名，复制到主人 wxid 里很容易复制成机器人自己。
  // 那样既认不出主人，主动搭话也没法「发给自己」（微信没有这个会话）。
  const accountId = stripAccountHash(config && config.wechat && config.wechat.account);
  if (accountId && wxids.some((w) => sameWxid(w, accountId))) {
    out.push(
      'master.wxids 里填的是机器人当前登录的账号（' + accountId + '），不是你自己；' +
      '微信里没法给自己发消息，主动搭话会改发文件传输助手。请换成你自己的 wxid'
    );
  }
  return out;
}

// 称呼可以按昵称兜底，管理权限必须绑定已配置的发送者 ID。
function isVerifiedOwner(config, msg) {
  const hit = matchMaster(config, msg);
  return hit.reason === 'wxid' && hit.senderId !== 'unknown';
}

module.exports = {
  matchMaster, masterWarnings, masterLists, looksLikeWxid, isVerifiedOwner,
  sameWxid, stripAccountHash,
};
