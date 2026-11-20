# 契约 A→C：发言决策线上字段适配（已冻结）

版本：`social-style-c1` / 适配器 `a2c-wire-v1`  
冻结日期：`2026-11-17`  
状态：已实现 `social_expression/adapters.py` 与 mock tests；生产接入未执行。  
主控单页：`CONTRACT_c_integration_one_pager.md`。

## 1. 线上格式

A 可以发送大写动作与 camelCase 字段：

```json
{
  "contract_version": "a2c-wire-v1",
  "action": "SPEAK",
  "score": 0.78,
  "reason": "addressed_by_member",
  "addressed": true,
  "ambientRelevance": 0.72,
  "ttlSeconds": 120
}
```

`contract_version` 可省略；出现时必须等于 `a2c-wire-v1`，否则按契约错误回落 `WAIT`。

C 内部统一为：

```json
{
  "action": "speak",
  "score": 0.78,
  "reason": "addressed_by_member",
  "addressed": true,
  "ambient_relevance": 0.72,
  "ttl_seconds": 120.0
}
```

## 2. 字段映射

| A 线上字段 | C 内部字段 | 规则 |
| --- | --- | --- |
| `action` = `SPEAK` / `WAIT` / `SKIP` | `action` = `speak` / `wait` / `skip` | 仅这三个值；大小写不敏感，输出必为小写；未知值 -> 契约错误 |
| `ttlSeconds` | `ttl_seconds` | 数值，`0..604800` 秒；缺省 `120`；负值或超范围 -> 契约错误 |
| `ambientRelevance` | `ambient_relevance` | 数值 `0..1`；缺省 `0`；越界 -> 契约错误 |
| `addressed` | `addressed` | 必须是 JSON `true/false`；字符串 `"true"`、数字 `1` 都拒绝 |
| `score` | `score` | 数值 `0..1`；缺省 `0`；越界 -> 契约错误 |
| `reason` | `reason` | 字符串，截断 160 字；非字符串转字符串；只用于日志，不参与决策 |
| `contract_version` | 版本校验 | 可省略；出现时必须等于 `a2c-wire-v1`，否则 `version_mismatch` -> `WAIT` |

若同时出现 snake_case 与 camelCase，**snake_case 优先**，避免双写漂移。

## 3. 失败策略

- `parse_upstream(payload)`：严格模式，非法字段抛 `ContractError`。
- `from_session_a(payload)`：生产用安全包装，任何非法/缺失/类型错误/版本不匹配都回落为：

```json
{"action": "wait", "score": 0.0, "reason": "upstream_contract_error:<code>", "addressed": false,
 "ambient_relevance": 0.0, "ttl_seconds": 0.0}
```

即**契约错误绝不升级为 SPEAK**。

## 4. TTL

- `is_upstream_expired(decision, age_seconds)`：仅对 `speak` 生效；`ttl_seconds <= 0` 视为已过期；`age > ttl` 视为已过期。
- `upstream_allows_send(decision, age_seconds)`：`speak` 且未过期才为真。
- 调用方必须在 A 产出决策时记录时间；C 不读时钟，避免不可重复测试。
- 过期决策不得重新解释成 `speak`；应重新向 A 取新决策或走旧路径。

## 5. 决策所有权与 C 的否决边界

A 拥有“说不说”的最终所有权：

- `WAIT` / `SKIP`：C 直接 `silence`，即使被点名、私聊、深夜也不翻案。
- `SPEAK`：C **不重新做话题相关性打分**，不得因为 `ambient_relevance` 低而静默；`low_relevance` 只在 A 未提供决策时作为保守兜底。

C 只允许在以下硬边界否决发送：

| 硬边界 | C 的行为 | 理由 |
| --- | --- | --- |
| 深夜非点名 | `silence` | 群内休息边界；点名则短而温和，不静默 |
| 地板拥挤/频率 | 非点名 `silence`；点名压缩为短答 | 连续抢话/刷屏 |
| 模板复读/重复提问 | 非点名 `silence`；点名要求换开头 | 复读与抢话 |
| 图片不可读 | 群内非点名 `silence`；不猜图 | 无事实依据 |
| 转发/助力内容 | 群内非点名 `silence` | 无 grounded basis |
| 危机/敏感/凭据攻击 | `silence` 或最短安全表达；禁止幽默/引用/贴纸/记忆 | 安全边界 |
| 技术预算 | 压缩长度/气泡，不改变事实 | 通道上限 |

除此之外，C 只输出 `StyleDecision`（长度、语气、幽默、引用、问题、贴纸意图、梗），不改变 A 的 `speak` 结论，不生成身份/事实/权限。

## 6. 针对性测试

`tests/test_adapters.py` 覆盖：

- `SPEAK/WAIT/SKIP` -> 小写；
- `ttlSeconds/ambientRelevance` -> snake_case；
- 双写时 snake_case 优先；
- 未知/缺失 action、非布尔 `addressed` -> `wait`；
- TTL 0/超时/超范围；
- A=`SPEAK` + 低相关 -> C 仍回复；
- A=`WAIT` + 被点名 -> C 仍静默；
- A=`SPEAK` + 地板/深夜/图片不可读 -> C 仍硬否决。

## 7. 不做什么

- 不修改生产 Swift/Python 文件；由主控在模型/人格层接 adapter。
- 不把 A 的 `score` 当作 C 的表达置信度；两者独立。
- 不把 `reason` 原文写入长期记忆或对外消息。
- 不在 C 内部重新判断“是否值得回复”。
