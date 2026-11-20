# 契约 B→C：`QQMemoryItem` → `MemoryFact` 适配（已冻结）

版本：`social-style-c1` / 适配器 `b2c-memory-v1`  
冻结日期：`2026-11-17`  
状态：已实现 `adapters.py` 与降级测试；生产接入未执行。  
主控单页：`CONTRACT_c_integration_one_pager.md`。  
C 不读 B 的记忆库，不接触原始聊天、真实群号或成员账号。

## 1. 真实结构（审计基线 `3675e58`）

`Sources/BotCore/QQMemoryBook.swift`：

```swift
public struct QQMemoryItem {
    public var id = UUID().uuidString
    public let subject: String          // OWNER / PEER / MEMBER_<hash>
    public let kind: String             // preference / task / episode / legacy
    public let text: String
    public let keywords: [String]
    public let importance: Int
    public let sourceIDs: [String]
    public let updatedAt: Date
    public let expiresAt: Date
    public var invalidatedAt: Date?
    public func current(_ now: Date) -> Bool { invalidatedAt == nil && expiresAt > now }
}
```

`QQMemoryBook` 是“一个账号 + 一个私聊/群”的一个 book；`subject` 由 `account + target + sender` 生成，群成员是 `MEMBER_<sha256 前 6 字节>`，主人是 `OWNER`，私聊对方是 `PEER`。  
**`subject` 本身不足以区分群**：两个群的 `OWNER` 都是 `OWNER`。所以 B 必须额外给 `scope_key`。

## 2. B 必须提供的 envelope

```json
{
  "contract_version": "b2c-memory-v1",
  "scope_key": "g_9f2c1a",
  "item": {
    "subject": "OWNER",
    "kind": "preference",
    "text": "喜欢简短回复，别追问住址",
    "keywords": ["简短"],
    "importance": 4,
    "sourceIDs": ["src-1"],
    "updatedAt": "2026-11-17T09:00:00Z",
    "expiresAt": "2026-11-18T09:00:00Z",
    "invalidatedAt": null
  }
}
```

- `contract_version`：可省略；出现时必须等于 `b2c-memory-v1`，否则丢弃。

- `scope_key`：B 用固定盐对 `account|target` 做不可逆摘要，<=64 字符；不得是真实群号、群名或账号。
- `item`：Swift `QQMemoryItem` 的 JSON 投影；日期可用 ISO8601 字符串或 unix 秒。
- C 只接收 envelope；缺失 `scope_key` 或与当前上下文不一致一律丢弃。

## 3. 字段映射

| `QQMemoryItem` | `MemoryFact` | 规则 |
| --- | --- | --- |
| `subject` | `subject_hash` | `sha256("dafeiyu-social-style-c1|scope_key|subject")[:16]`；不保留原文 |
| `scope_key`（envelope） | 参与 `subject_hash` | 跨群同一 `OWNER` 也会得到不同 hash |
| `text` | `fact` | `redact()` 后截断 <=160 字；空文本丢弃 |
| `kind` | `citable` | `preference` / `task` 且 `sourceIDs` 非空才可引用；`episode` / `legacy` 不可引用 |
| `importance` | `confidence` | `1..5` 映射 `0.5..0.9`；缺失按 1 处理；非整数丢弃 |
| `sourceIDs` | `citable` | 非空才视为有证据；缺失/空 -> 不可引用 |
| `expiresAt` | `valid_for_seconds` | `expiresAt - now`；<=0 丢弃；缺失丢弃 |
| `invalidatedAt` | 不生成 | 非空 -> 已删除，丢弃 |
| `keywords` | 不使用 | C 不把关键词注入提示，避免扩写 |
| `id` / `updatedAt` | 不使用 | 不暴露给提示；只用于 B 内部 |

`MemoryFact` 永远不含原始 `subject`、群名、账号、聊天原文；`subject_hash` <=32 字符。

## 4. 人称与跨群

- 群聊：`expected_subject` 必须是当前发送者的 subject（主人为 `OWNER`，成员为 `MEMBER_<hash>`）。不等于即 `wrong_subject`，丢弃。
- 私聊：只接受 `PEER` 或 `OWNER`；群聊里出现 `PEER`、私聊里出现 `MEMBER_*` 都丢弃。
- `CONVERSATION` / `kind=episode` 是会话摘要，不作为个人事实；`citable=false`，C 的 `filter_memory_facts` 会丢弃。
- `scope_key` 不一致 -> `scope_mismatch`；不得把 A 群的事实带到 B 群。
- 同一 `OWNER` 在两个群得到不同 `subject_hash`；测试覆盖。

## 5. 失败回退

| 情况 | 结果 | reason |
| --- | --- | --- |
| `contract_version` 不匹配 | `None` | `version_mismatch` |
| 缺 `scope_key` / 与当前不一致 | `None` | `scope_mismatch` / `scope_key_missing` |
| 缺 `subject` / 与当前发送者不一致 | `None` | `wrong_subject` |
| 缺 `expiresAt` | `None` | `missing_expiry` |
| `expiresAt <= now` | `None` | `expired` |
| `invalidatedAt` 非空 | `None` | `invalidated` |
| 缺 `text` / `kind` | `None` | `text_missing` / `kind_missing` |
| `importance` 缺失 | 保留事实，confidence `0.5` | — |
| `sourceIDs` 缺失/空 | 保留事实，`citable=false` | — |
| 批量中部分非法 | 只保留合法项 | — |

安全包装：

```python
fact = adapt_qq_memory_item(envelope, current_scope_key=..., expected_subject=..., now_epoch=...)
# None -> 本轮不注入该事实，不阻断回复
```

严格模式 `qq_memory_fact()` 抛 `ContractError`，供测试/日志；生产用 `adapt_qq_memory_item()` 做 fail-closed。

## 6. 测试

`tests/test_adapters.py` 覆盖：

- 正常映射：`subject_hash`、`fact<=160`、`confidence`、`valid_for_seconds`、`citable`；
- 同一 `OWNER` 跨群 hash 不同；
- `scope_key` 不一致；
- 错误人称（`PEER` vs `OWNER`）；
- 缺 expiry、已过期、已删除（`invalidatedAt`）；
- 缺 text/kind；
- 缺 importance -> 0.5；
- 无 `sourceIDs` -> 不可引用；
- `episode` -> 不可引用；
- 敏感 token 脱敏；
- 批量混合过滤。

## 7. 红线

- 不接触原始真实聊天：C 只接收 B 已脱敏、已摘要的 `QQMemoryItem` 投影。
- 不持久化：C 不写 B 的记忆库，不缓存事实，不把 `subject_hash` 写回。
- 不回传原文：`StyleMemoryDigest` 永远 `citable=false`、`raw_text=null`。
- 不跨群：`scope_key` 必须匹配，且参与 `subject_hash`。
- 不把 `episode`/`CONVERSATION` 摘要包装成“某人说过”的个人事实。
