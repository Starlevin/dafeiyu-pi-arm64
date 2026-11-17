'use strict';
// PersonaGuard v1 — CommonJS mirror of qq-core/Sources/BotCore/PersonaGuard.swift.
// Detection only. It never calls a model and never rewrites semantics.

const CODES = Object.freeze({
  CLAIMS_HUMAN: 'claims_human', DENIES_AI: 'denies_ai', AI_DISCLAIMER: 'ai_disclaimer',
  FABRICATED_SHARED_EXPERIENCE: 'fabricated_shared_experience',
  FABRICATED_RELATIONSHIP: 'fabricated_relationship',
  OVER_FAMILIAR_ADDRESS: 'over_familiar_address', REPEATED_TIC: 'repeated_tic',
  CUSTOMER_SERVICE_TONE: 'customer_service_tone', AI_SLOP_CONNECTOR: 'ai_slop_connector',
  EXCESSIVE_LENGTH: 'excessive_length', MARKDOWN_FORMATTING: 'markdown_formatting',
  STAGE_DIRECTION: 'stage_direction', FABRICATED_COMPETENCE: 'fabricated_competence',
  ROLE_BREAK: 'role_break', PUNCTUATION_SPAM: 'punctuation_spam', EMPTY_TEXT: 'empty_text',
  PLACEHOLDER: 'placeholder', REPEATED_REPLY: 'repeated_reply',
});
const SEVERITY = Object.freeze({ BLOCK: 'block', REWRITE: 'rewrite', WARN: 'warn' });
const SEVERITY_RANK = Object.freeze({ block: 0, rewrite: 1, warn: 2 });
const SCENARIOS = Object.freeze(['greeting', 'casual', 'help', 'banter', 'serious', 'comfort', 'identity']);
const OPENER_NAMES = Object.freeze(['直答', '短评', '反问', '接梗', '确认']);

const RULES = [
  [CODES.CLAIMS_HUMAN, SEVERITY.BLOCK, /我是(?:一个)?(?:真人|人类|活人|现实里的人)(?!吗|么|？|\?)|我(?:可|肯|确实)是(?:真人|人类)(?!吗|么|？|\?)|我(?:不是|才不是)(?:AI|机器人|人工智能)|我(?:有|在现实里有)(?:肉身|实体身体)|现实(?:里|中)的我/i, '自称真人'],
  [CODES.DENIES_AI, SEVERITY.BLOCK, /我不是(?:一个)?(?:AI|人工智能|语言模型|机器人)/i, '否认 AI 身份'],
  [CODES.AI_DISCLAIMER, SEVERITY.BLOCK, /我是(?:一个)?(?:AI|人工智能)(?:助手|语言模型|机器人)|作为(?:一个)?(?:AI|人工智能|语言模型|助手)|我只是(?:一个)?(?:AI|语言模型)/i, 'AI 助手腔'],
  [CODES.FABRICATED_RELATIONSHIP, SEVERITY.BLOCK, /(?:做|当)我(?:女朋友|男朋友|老婆|老公|对象)|你是我(?:的人|女朋友|男朋友|老婆|老公|对象)|永远(?:在一起|不分开)/i, '虚构恋爱/占有关系'],
  [CODES.FABRICATED_COMPETENCE, SEVERITY.BLOCK, /我已经(?:帮你|给你|替你)(?:重启|部署|删除|下单|付款|转账|发送|执行|修好)|我(?:帮你|替你)(?:重启|部署|删除|下单|付款|转账)(?:了|好了)|我(?:已经)?(?:关掉|打开)了你的/i, '声称执行了系统操作'],
  [CODES.ROLE_BREAK, SEVERITY.BLOCK, /系统提示词|system\s*prompt|开发者模式|越狱模式|jailbreak/i, '泄露内部规则'],
  [CODES.PLACEHOLDER, SEVERITY.REWRITE, /\{\{[^}]+\}\}|\[你的名字\]|TODO|<\|[^|]+\|>/i, '未替换占位符'],
  [CODES.CUSTOMER_SERVICE_TONE, SEVERITY.REWRITE, /有什么可以帮(?:您|你)|请问还有(?:什么|其他)(?:可以|需要)|很高兴为您服务|感谢您的咨询|不客气[，,]?这是我应该做的/i, '客服腔'],
  [CODES.AI_SLOP_CONNECTOR, SEVERITY.REWRITE, /首先[，,]|其次[，,]|综上所述|总的来说|希望对你有帮助|如果你有任何问题|不难看出|值得注意的是/i, 'AI 连接词'],
  [CODES.STAGE_DIRECTION, SEVERITY.REWRITE, /（[^）]{0,12}(?:笑|叹气|摇头|挥手|歪头|眨眼|摊手)[^）]{0,12}）|\([^)]{0,12}(?:laughs|sighs|smiles)\)/i, '括号动作描写'],
];

function countMatches(pattern, text) {
  const re = new RegExp(pattern.source, pattern.flags.includes('g') ? pattern.flags : pattern.flags + 'g');
  const matches = text.match(re);
  return matches ? matches.length : 0;
}
function firstMatch(pattern, text) {
  const m = text.match(pattern);
  return m ? m[0] : null;
}
function normalizeText(text) {
  return String(text).toLowerCase().replace(/[\s\p{P}\p{S}]/gu, '');
}

function makeProfile(raw = {}) {
  const p = Object.assign({ displayName: '大肥鱼', relationship: 'peer', maxCharacters: 60, ticBudget: 1, emojiBudget: 1, allowTeasing: true }, raw);
  p.relationship = ['owner', 'peer', 'stranger'].includes(p.relationship) ? p.relationship : 'peer';
  p.maxCharacters = Math.max(10, Math.min(200, Number(p.maxCharacters) || 60));
  p.ticBudget = Math.max(0, Math.min(3, Number(p.ticBudget) || 0));
  p.emojiBudget = Math.max(0, Math.min(2, Number(p.emojiBudget) || 0));
  p.allowTeasing = Boolean(p.allowTeasing);
  return p;
}

function makeContext(raw = {}) {
  return {
    hasMemoryEvidence: Boolean(raw.hasMemoryEvidence),
    recentReplies: Array.isArray(raw.recentReplies) ? raw.recentReplies : [],
  };
}

function stylePlan(profileRaw, scenario, seed) {
  const profile = makeProfile(profileRaw);
  const base = {
    greeting: [6, 24, 1, 1, 1], casual: [8, 36, 1, 2, 1], help: [20, 80, 2, 4, 0],
    banter: [8, 30, 1, 2, 1], serious: [20, 90, 2, 4, 0], comfort: [12, 50, 1, 3, 1],
    identity: [4, 20, 1, 2, 0],
  }[scenario] || [8, 36, 1, 2, 1];
  const rng = makeLcg(seed);
  const spread = rng.int(3);
  let minChars = base[0] + spread * 2;
  let maxChars = Math.min(base[1], profile.maxCharacters) - (2 - spread) * 2;
  if (maxChars < minChars) { minChars = maxChars; maxChars = Math.max(minChars, maxChars); }
  if (maxChars < 4) { minChars = 4; maxChars = 4; }
  let minSent = base[2];
  let maxSent = base[3];
  if (scenario === 'casual' || scenario === 'banter') {
    const extra = rng.int(2);
    minSent = 1 + extra;
    maxSent = minSent + 1;
  }
  const emoji = Math.min(profile.emojiBudget, base[4]);
  const tic = scenario === 'banter' ? Math.min(profile.ticBudget, 1) : 0;
  const opener = rng.int(OPENER_NAMES.length);
  const tease = profile.allowTeasing && (scenario === 'banter' || scenario === 'casual');
  const directive = `本轮约 ${minSent}–${maxSent} 句、${minChars}–${maxChars} 字；最多 ${tic} 个口头禅、${emoji} 个表情；开头用「${OPENER_NAMES[opener]}」；不写括号动作、Markdown、客服腔；不虚构共同经历或现实身份。`;
  return { minCharacters: minChars, maxCharacters: maxChars, minSentences: minSent, maxSentences: maxSent, emojiBudget: emoji, ticBudget: tic, allowTeasing: tease, openerFamily: opener, directive };
}

function check(textRaw, profileRaw, scenario, contextRaw = {}) {
  const profile = makeProfile(profileRaw);
  const context = makeContext(contextRaw);
  const text = String(textRaw == null ? '' : textRaw).trim();
  const hits = [];
  if (!text) return result(text, [{ code: CODES.EMPTY_TEXT, severity: SEVERITY.BLOCK, detail: '空文本' }]);
  const len = Array.from(text).length;
  if (len > profile.maxCharacters) hits.push({ code: CODES.EXCESSIVE_LENGTH, severity: SEVERITY.REWRITE, detail: `${len}>${profile.maxCharacters}字` });
  if (/(?:^|\n)\s*(?:[-*]\s+|\d+\.\s+)/m.test(text)) hits.push({ code: CODES.MARKDOWN_FORMATTING, severity: SEVERITY.REWRITE, detail: '列表/分段' });
  if (text.includes('**') || text.includes('```') || text.includes('|--')) hits.push({ code: CODES.MARKDOWN_FORMATTING, severity: SEVERITY.REWRITE, detail: 'Markdown 标记' });

  for (const [code, severity, pattern, detail] of RULES) {
    if (firstMatch(pattern, text)) hits.push({ code, severity, detail });
  }

  if (profile.relationship !== 'owner' && /主人/.test(text)) hits.push({ code: CODES.OVER_FAMILIAR_ADDRESS, severity: SEVERITY.BLOCK, detail: '非 OWNER 称主人' });
  if (/亲爱的|宝贝|宝宝|老婆|老公/.test(text)) {
    hits.push({ code: CODES.OVER_FAMILIAR_ADDRESS, severity: profile.relationship === 'owner' ? SEVERITY.WARN : SEVERITY.REWRITE, detail: '亲密称呼越界' });
  }

  const ticCount = countMatches(/喵/, text);
  if (ticCount > profile.ticBudget) hits.push({ code: CODES.REPEATED_TIC, severity: SEVERITY.REWRITE, detail: `喵 x${ticCount}` });
  const memeCount = countMatches(/[绷典孝急赢麻乐]/, text);
  if (memeCount > 2) hits.push({ code: CODES.REPEATED_TIC, severity: SEVERITY.REWRITE, detail: `梗字 x${memeCount}` });
  for (const token of ['杂鱼', '本鱼', '哼']) {
    if (countMatches(new RegExp(escapeRegExp(token), 'g'), text) > 1) hits.push({ code: CODES.REPEATED_TIC, severity: SEVERITY.REWRITE, detail: `${token} 复读` });
  }

  let emojiCount = 0;
  for (const ch of Array.from(text)) {
    const cp = ch.codePointAt(0);
    if ((cp >= 0x1F300 && cp <= 0x1FAFF) || (cp >= 0x2600 && cp <= 0x27BF)) emojiCount += 1;
  }
  if (emojiCount > profile.emojiBudget) hits.push({ code: CODES.PUNCTUATION_SPAM, severity: SEVERITY.WARN, detail: `表情 ${emojiCount}>${profile.emojiBudget}` });
  if (/[!！]{3,}|[?？]{3,}|[~～]{3,}/.test(text)) hits.push({ code: CODES.PUNCTUATION_SPAM, severity: SEVERITY.WARN, detail: '标点连打' });

  if (!context.hasMemoryEvidence && /记得(?:我们|你)|上次(?:我们|你)|以前(?:我们|你)/.test(text) &&
      !hits.some((h) => h.code === CODES.FABRICATED_SHARED_EXPERIENCE)) {
    hits.push({ code: CODES.FABRICATED_SHARED_EXPERIENCE, severity: SEVERITY.BLOCK, detail: '无记忆证据的回忆' });
  }

  const normalized = normalizeText(text);
  if (normalized && context.recentReplies.map(normalizeText).includes(normalized)) {
    hits.push({ code: CODES.REPEATED_REPLY, severity: SEVERITY.REWRITE, detail: '与近期回复完全重复' });
  }
  return result(text, hits);
}

function result(text, hits) {
  const sorted = hits.slice().sort((a, b) => (SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]) || a.code.localeCompare(b.code));
  return {
    passed: sorted.length === 0,
    hits: sorted,
    blocking: sorted.some((h) => h.severity === SEVERITY.BLOCK),
    rewriteRequired: sorted.some((h) => h.severity === SEVERITY.REWRITE),
    checkedCharacters: Array.from(text).length,
  };
}

function trim(textRaw, maxCharacters) {
  const text = String(textRaw == null ? '' : textRaw).trim();
  const chars = Array.from(text);
  if (chars.length <= maxCharacters) return text;
  let cut = chars.slice(0, maxCharacters).join('');
  const terminators = ['。', '！', '？', '；', '，', ','];
  let last = -1;
  const cutChars = Array.from(cut);
  for (let i = cutChars.length - 1; i >= 0; i -= 1) if (terminators.includes(cutChars[i])) { last = i; break; }
  if (last >= 0) {
    const keep = cutChars.slice(0, last).join('').trim();
    if (keep) return keep + '。';
  }
  return cut + '…';
}

function escapeRegExp(value) { return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function makeLcg(seed) {
  let state = BigInt(Math.trunc(Number(seed) || 0));
  const MASK = (1n << 64n) - 1n;
  return {
    int(upperBound) {
      if (!upperBound || upperBound <= 0) return 0;
      state = (state * 6364136223846793005n + 1442695040888963407n) & MASK;
      return Number(state % BigInt(upperBound));
    },
  };
}

module.exports = { CODES, SEVERITY, SCENARIOS, OPENER_NAMES, stylePlan, check, trim, makeProfile, makeContext };
