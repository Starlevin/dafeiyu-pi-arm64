# 契约 A：会话 A（人格与发言决策）↔ 会话 C（表达风格）

版本：`social-style-c1`  
状态：提案，待会话 A 确认后由主会话合并。双方不写同一源文件。

## 1. 职责边界

| 决策 | 归属 | 说明 |
| --- | --- | --- |
| 这轮要不要说话（SPEAK / WAIT / SKIP） | 会话 A | 群热度、被点名、话题相关性、人格主动度 |
| 这轮用什么人格/身份/事实 | 会话 A + 人格配置 | C 不生成身份、不改人设 |
| 这轮说多长、什么语气、是否引用/反问/幽默/贴纸 | 会话 C | 只消费 A 的结论，不重新打分 |
| 群内梗是否可用 | 会话 B 提供白名单，C 只做本群匹配 | C 不持久化原始聊天 |
| 最终发送目标、@对象、审核 | 现有 QQ/微信通道 + tx_guard | C 只给结构化建议 |

C **不能**把 A 的 `speak` 改成 `wait` 用于普通内容判断；只允许在以下硬约束下否决：`floor_guard`（连续抢话/频率）、`repeat_guard`（模板复读/重复提问）、`quiet_hours`（群内非点名深夜）、`low_relevance`（仅当 A 未提供决策时的保守兜底）。否决必须带 `reason_codes`，供 A 复盘。

## 2. 会话 A → C 的最小协议

字段名与 `social_expression.models.UpstreamDecision` 一致：

```json
{
  "action": "speak",
  "score": 0.78,
  "reason": "addressed_by_member",
  "addressed": true,
  "ambient_relevance": 0.72,
  "ttl_seconds": 120
}
```

- `action`：`speak` / `wait` / `skip`；`wait` 与 `skip` 都视为本轮不发言。
- `score`、`ambient_relevance`：0..1。C 只读，不重新计算语义相关性。
- `addressed`：A 是否判定本轮被点名/回复机器人/主人直呼；C 仍会独立校验 `mentioned_bot` 等硬信号，防止 A 误判。
- `ttl_seconds`：超过 TTL 的决策不得复用；由调用方保证新鲜度。
- 可选 `decision_id`（字符串，<=64）：用于日志关联；C 不持久化。

会话 A 不需要知道 `length/tone/sticker` 等字段；这些由 C 产出。

## 3. C → 模型/通道的输出

字段名与 `social_expression.models.StyleDecision` 一致：

```json
{
  "action": "reply",
  "scenario": "serious_help",
  "length": "medium",
  "tone": "neutral",
  "humor": "none",
  "quote": "none",
  "question": false,
  "sticker_intent": "none",
  "max_chars": 58,
  "max_bubbles": 1,
  "voice": false,
  "visual_grounding_required": false,
  "rewrite_required": false,
  "memory_allowed": true,
  "meme_term": null,
  "avoid_recent_phrases": [],
  "reason_codes": ["scene:serious_help"],
  "confidence": 0.8,
  "profile_version": "social-style-c1"
}
```

- `action=silence` 时，调用方必须不生成、不发送；`style_prompt_constraints()` 返回空串。
- `sticker_intent` 只是意图：`none` 禁止配图；`optional` 允许选择层放弃；`preferred` 仍要过授权/冷却/相关性，不能强塞。
- `quote` 是引用意图，不是引用原文；正文不得复述敏感原文。
- `max_chars/max_bubbles` 是硬上限；模型输出超过时由调用方截断或重写，不得静默超发。
- `rewrite_required` 为真时，调用方必须换开头/句式，不能复用 `avoid_recent_phrases`。

## 4. 调用顺序（建议）

```
入站消息
  -> 会话 A: ReplyDecision (speak/wait/skip)
  -> 会话 C: decide(context) -> StyleDecision
  -> 人格提示 + C.style_prompt_constraints(...)
  -> 模型生成
  -> C.is_too_similar(draft, recent_bot_texts)
       命中：带约束重生成 1 次；仍命中：群聊静默 / 私聊最短安全答复
  -> 贴纸选择层（可选）
  -> 现有通道审核 + tx_guard + 发送
  -> 回执
```

C 的单次 `decide` 是纯函数：无网络、无模型、无文件、无时钟。调用方负责把时间转成 `seconds_ago`、`local_hour`；`recent_messages` 与 `bot_replies` 都必须**排除当前这条入站/本轮草稿**，否则复读检测会把当前消息和自己比较。

## 5. 失败与回滚

- `DAFEIYU_SOCIAL_STYLE=0`（默认）：C 不参与，A 与旧人格路径完全不变。
- `DAFEIYU_SOCIAL_STYLE_SHADOW=0`（默认）：不做影子记录；`=1` 时只计算/记录 `StyleDecision`，不改提示、不改发送；用于灰度观察。
- C 抛异常：调用方 `try/except` 后继续旧路径，不得阻断回复；记录一次 `social_style_error` 计数。
- 回滚：环境变量置 0 或删除 `integrations/social-expression/`；无数据库迁移、无新增进程、无定时任务。

## 6. 文件所有权

- 会话 A：`QQReplyPolicy`、`ReplyDecision`、群热度/主动度相关文件。
- 会话 C：`integrations/social-expression/**`、本契约、测试与报告。
- 双方都不直接改对方文件；需要改接口时先提 PR/契约变更，由主会话裁决。
