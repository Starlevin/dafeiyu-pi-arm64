# 微信侧接入补丁（STAGED，未应用）

状态：**STAGED**。本文件是给主会话审核的补丁提案；没有修改 `2026-11-15-微信群修复-合并审核/merged/core` 的任何文件。
可独立运行/单测的模块已在 `wechat-core/src/replyDecision.js`、`wechat-core/src/personaGuard.js`。

## 0. 文件放置

把两个模块复制到生产 core（保持 CommonJS、零依赖）：

```
merged/core/src/core/replyDecision.js   ← wechat-core/src/replyDecision.js
merged/core/src/core/personaGuard.js    ← wechat-core/src/personaGuard.js
```

## 1. 开关

```js
// 默认 off：不加载、不改变任何现有行为。
// shadow：只记录协议判定，仍由 proactiveJudge 决定是否发言。
// enforce：协议 SKIP/WAIT 时直接跳过 proactiveJudge（仍保留所有原有安全闸门）。
const REPLY_DECISION_MODE = String(process.env.DAFEIYU_WECHAT_REPLY_DECISION_V2 || 'off').toLowerCase();
const PERSONA_GUARD_MODE = String(process.env.DAFEIYU_WECHAT_PERSONA_GUARD || 'off').toLowerCase();
```

`enforce` 必须等“共享评分器”导出后再开：当前 `proactiveJudge.judge` 内部私有评分，协议若用 `scorer=null` 会退化成 heuristic-only（回放匹配率 0.82，过于保守）。第一阶段只建议 `shadow`。

## 2. 插在 `maybeJudgeProactive` 的 `proactiveJudge.judge` 之前

位置：`src/pipeline/chat.js`，`this._proactiveJudgeState = ...` 之后、`const result = await proactiveJudge.judge({...})` 之前。

```js
const replyDecision = require('../core/replyDecision');
const personaGuard = require('../core/personaGuard');

// inside maybeJudgeProactive, after judgeCfg and state are known:
if (REPLY_DECISION_MODE !== 'off') {
  const decision = this._replyDecisionPreGate({ msg, rows, current, judgeCfg, nowMs });
  if (this.logger) {
    this.logger.info('[wechat_reply_decision] mode=' + REPLY_DECISION_MODE
      + ' action=' + decision.action + ' reason=' + decision.reason
      + ' score=' + decision.score + ' source=' + decision.source
      + ' scorer=' + decision.scorerCalled);
  }
  // 只收紧、不放宽：enforce 下协议认为不该说，就不进入现有 judge。
  if (REPLY_DECISION_MODE === 'enforce' && decision.action !== replyDecision.ACTIONS.SPEAK) {
    return { allow: false, reason: 'reply_decision_' + decision.reason };
  }
}

const result = await proactiveJudge.judge({ /* 原参数完全不动 */ });
```

### `_replyDecisionPreGate` 映射（放在 Bot 类内）

```js
_replyDecisionPreGate({ msg, rows, current, judgeCfg, nowMs }) {
  const group = String(msg.chatTitle || '');
  const key = this.sessionKey(msg);
  // 复用现有 judge 的群级计数作为同一预算账本；协议本身不 reserve、不重复扣。
  this._replyDecisionState = this._replyDecisionState || {};
  const st = this._replyDecisionState[key] || {};
  const budget = Math.max(1, Number(judgeCfg.dailyBudget) || 60);
  const judgeCount = Number((((this._proactiveJudgeState || {}).groups || {})[group] || {}).count) || 0;
  const lastBot = (rows || []).slice().reverse().find((r) => r && (r.role === 'assistant' || r.isBot === true));
  const lastAt = lastBot ? Number(lastBot.ts || lastBot.at || 0) : 0;
  const lastText = lastBot ? String(lastBot.text || '') : '';
  const text = String((current && current.text) || '');
  const sinceBot = lastAt ? Math.max(0, Math.floor((nowMs - lastAt) / 1000)) : 86400;
  const q = ['?', '？', '吗', '怎么', '为什么', '谁', '哪里', '多少', '能不能', '求', '帮我看', '有没有'].some((x) => text.includes(x));
  const window = (rows || []).slice(-8).map((r) => String((r && r.text) || ''));
  const input = {
    scope: 'group',
    chatAlias: key,
    window,
    mention: 'none',                 // 本路径只处理非 @ 主动候选
    unansweredQuestion: Boolean(current && current.unanswered),
    questionToAll: q,
    invited: false,
    addressedToOther: false,         // 能走到 groupAutoReply 分支即已排除点名他人
    botSpokeRecently: sinceBot < 90,
    secondsSinceBotReply: sinceBot,
    secondsSinceTrigger: Math.max(0, Math.floor((nowMs - Number((current && current.ts) || nowMs)) / 1000)),
    repeatedTopic: false,            // 如需重复检测，用下面 bigram 与 lastText 比较
    interruptCost: 0.35,
    topicRelevance: q ? 0.85 : 0.45,
    isOwner: false,
    quietHours: Boolean(this.config && this.config.group && this.config.group.quietHours),
    dailyRemaining: Math.max(0, budget - judgeCount),
  };
  const state = Object.assign({ dayKey: replyDecision.dayKeyFor(Math.floor(nowMs / 1000)), decisions: 0, speaks: 0,
    scorerCalls: 0, lastSpeakAt: 0, lastDecisionAt: 0, recentSpeakTexts: [] }, st);
  const next = Object.assign({}, state);
  // scorer 传 null：第一阶段只做启发式预筛；真实评分仍由 proactiveJudge 负责。
  const result = replyDecision.decide(input, state, Math.floor(nowMs / 1000), null, {
    dailyLimit: budget, threshold: Number(judgeCfg.threshold) || 0.70,
  });
  this._replyDecisionState[key] = next; // decide 不修改传入 state；此处仅保留 dayKey 等元数据
  return result;
}
```

注意：`decide` 返回 `{action, score, reason, ttlSeconds, source, heuristicScore, scorerCalled}`；它是纯函数，不 reserve 预算。所以上面的 `judgeCount` 与 `recordSpeak` 必须由适配层维护，不能双扣。

## 3. PersonaGuard：生成之后、发送之前

位置：`chat.js` 里最终回复文本确定之后、调用发送之前（所有 `action: 'reply'` 的实际发送点之前统一加一层中间件最稳）。

```js
function applyPersonaGuard(text, { isOwner, scenario }) {
  if (PERSONA_GUARD_MODE === 'off') return { text, blocked: false };
  const profile = {
    displayName: '大肥鱼',
    relationship: isOwner ? 'owner' : 'peer',
    maxCharacters: 120,
    ticBudget: 1,
    emojiBudget: 1,
    allowTeasing: !isOwner,
  };
  const check = personaGuard.check(String(text || ''), profile, scenario || 'casual',
    { hasMemoryEvidence: false, recentReplies: [] });
  if (check.blocking) {
    return { text: '', blocked: true, codes: check.hits.filter((h) => h.severity === 'block').map((h) => h.code) };
  }
  return { text, blocked: false, check };
}
```

接入原则：

- `PERSONA_GUARD_MODE=proactive` 只检查“非 @ 主动候选”；`all` 才检查所有回复。默认 `off`，避免误杀正常答题。
- `hasMemoryEvidence: false` 是 POC 保守值：主动回复不许声称“我记得我们上次……”。真实记忆回复应在私聊/直接回答路径，不经过本 guard。
- block 时返回 `{action:'ignore', reason:'persona_guard'}`，并记录 `codes`（不记录正文）。

## 4. `recordSpeak`：只在发送成功后

在发送成功回调/返回值处：

```js
if (REPLY_DECISION_MODE !== 'off' && sentOk) {
  const key = this.sessionKey(msg);
  this._replyDecisionState = this._replyDecisionState || {};
  const now = Math.floor(this.now().getTime() / 1000);
  const state = this._replyDecisionState[key] || { dayKey: replyDecision.dayKeyFor(now), decisions: 0, speaks: 0,
    scorerCalls: 0, lastSpeakAt: 0, lastDecisionAt: 0, recentSpeakTexts: [] };
  replyDecision.recordSpeak(text, now, state);
  this._replyDecisionState[key] = state;
}
```

失败、被拦截、被 judge 拒绝都不调用；不要“先记后发”。

## 5. 回滚

| 变量 | 回滚值 | 效果 |
|---|---|---|
| `DAFEIYU_WECHAT_REPLY_DECISION_V2` | `off` / 删除 | 完全不进入协议，`judge` 原路径不变 |
| `DAFEIYU_WECHAT_PERSONA_GUARD` | `off` / 删除 | 不检查人格，原发送路径不变 |
| 删除 `require` 与两处调用 | — | 代码级回滚，无数据迁移 |

模块不写盘、不改现有账本、不加依赖、不开端口，回滚只影响本次进程。

## 6. 测试计划（应用补丁后）

1. `node --test wechat-core/test/*.test.js`：28/28 应保持。
2. 用 `replay/run_replay.js --mode=fixture` 作为回归；协议行为不变。
3. 在测试群 `DAFEIYU_WECHAT_REPLY_DECISION_V2=shadow` 观察 `[wechat_reply_decision]` 日志 24h：
   - `action=SKIP` 且 `judge allow=true` 的比例（协议是否过度保守）
   - `persona_guard` block 次数与 code 分布
   - 不改变实际发言量。
4. 通过后再考虑 `enforce`；enforce 前必须把 `proactiveJudge` 的评分函数导出为 `scoreWindow(...)`，让协议与 judge 共用同一次评分，避免双调用/双预算。

## 7. 已知风险

- `judge` 自带 `dailyBudget` 与 `cooldown`；协议若再独立计数会双扣。补丁用同一个 `_proactiveJudgeState.groups[group].count` 作为 `dailyRemaining` 来源。
- `rows` 的字段名（`role/isBot/ts/text`）因桥接版本而异；接入前用真实 `store.messages` 打印一条脱敏样本核对，不要假设。
- `current.unanswered`、`quietHours` 目前不一定存在；缺失时按 `false` 处理，宁可少说。
- 本补丁不覆盖“直接 @ / 私聊必答”，那两条路径本来就不进 `groupAutoReply`，不需要也不应该被协议拦截。
