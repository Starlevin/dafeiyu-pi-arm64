# 契约冻结：C 社交表达 / 通道协议单页集成指南

版本：`social-style-c1`  
适配器版本：A→C `a2c-wire-v1`，B→C `b2c-memory-v1`  
通道协议：`c-transport-v1`  
冻结日期：`2026-11-17`  
状态：**供主控消费；生产接入未执行，真实群 E2E = NOT_RUN。**

代码常量：`social_expression/version.py`。  
测试基线：旧 96 项不变；新增协议/预算测试单独计数（见 `reports/transport_protocol_freeze.md`）。

## 1. 接入顺序

```text
现有 QQ/微信“要不要回复”判定（A）
        -> from_session_a() / upstream_allows_send()
        -> decide()（C 只决定表达形状；硬边界可否决）
        -> 现有模型生成 + 复读检查
        -> build_*() 只生成 payload 草案
        -> target guard / tx_guard / 现有发送器（主控执行）
```

C 不建立 socket、不调用 OneBot、不调用微信 Bridge、不常驻、不读时钟、不写记忆库。

## 2. A→C：字段与 fail-closed

### 2.1 输入

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

- `contract_version` 可省略；出现时必须等于 `a2c-wire-v1`，否则 `ContractError("version_mismatch")`。
- `action` 只接受 `SPEAK / WAIT / SKIP`（大小写不敏感），输出 `speak / wait / skip`。
- `ttlSeconds` → `ttl_seconds`，`0..604800`；缺省 120。
- `ambientRelevance` → `ambient_relevance`，`0..1`；缺省 0。
- `addressed` 必须是 JSON bool；字符串/数字拒绝。
- snake_case 与 camelCase 双写时，snake_case 优先。

### 2.2 失败策略

```python
decision = from_session_a(payload)
if not upstream_allows_send(decision, age_seconds):
    return legacy_no_send_path()     # WAIT/SKIP、TTL<=0、TTL 过期都不发送
```

- 任何缺失/未知 action/类型错误/版本不匹配 → `WAIT`，绝不升级成 `SPEAK`。
- `WAIT` / `SKIP` 是最终所有权：C 不因点名、私聊、高相关而翻案。
- `SPEAK` 时 C 不再按低相关静默；只允许硬边界否决：深夜非点名、地板/频率、复读/重复提问、图片不可读、转发无依据、危机/敏感/凭据攻击、通道预算。
- 过期 TTL 只能重新向 A 取新决策，不得把旧 `SPEAK` 重放。

## 3. B→C：`QQMemoryItem` envelope

B 必须发送 envelope，不能只发 `QQMemoryItem`：

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

规则：

| 字段 | C 的处理 |
| --- | --- |
| `contract_version` | 可省略；出现时必须等于 `b2c-memory-v1` |
| `scope_key` | 必填；必须等于当前会话 scope；不得是真实群号/群名 |
| `subject` | 必须等于 `expected_subject`；群内 `PEER`、私聊 `MEMBER_*` 拒绝 |
| `text` | `redact()` 后截断 160 字；空文本丢弃 |
| `kind` | `preference/task` 且 `sourceIDs` 非空才 `citable`；`episode/legacy` 不引用 |
| `importance` | 1..5；缺失按 1（confidence 0.5） |
| `sourceIDs` | 缺失/空 → `citable=false`，不阻断回复 |
| `expiresAt` | 必填；`<= now` 丢弃，缺失丢弃 |
| `invalidatedAt` | 非空 → 已删除，丢弃 |
| `id/updatedAt/keywords` | C 不使用，不写入提示 |

```python
fact = adapt_qq_memory_item(
    envelope,
    current_scope_key=current_scope_key,
    expected_subject=current_subject,
    now_epoch=now_epoch,
)
if fact is not None and fact.citable:
    facts = filter_memory_facts((fact,))
```

- 跨群/scope 不一致 → `None`；不得把 A 群事实带到 B 群。
- `subject_hash = sha256("dafeiyu-social-style-c1|scope_key|subject")[:16]`；不保留原始 subject。

## 4. 精确聊天目标 ID

### QQ

| 目标 | 规则 |
| --- | --- |
| 群 | `group_id` 正整数，十进制精确相等；禁止按群名/近似群名发送 |
| 私聊 | `user_id` 正整数，精确相等 |
| @ | `at_qq` 5–12 位数字；`mentioned_bot=false` 时不得附带 at |
| 回复 | `reply_to` 匹配 `[A-Za-z0-9:_-]{1,64}`；只构造 reply segment |
| 撤回 | `build_qq_recall` 生成 OneBot `delete_msg`；发送前仍用目标守卫生效 |

### 微信

- `target_exact` = NFKC 后的当前会话标题；**保留 `测试群(5)` 的成员数后缀**。
- `测试群(4)` 与 `测试群(5)` 不相等；不得剥后缀后比较。
- 屏幕外同名、标题不可读、`visible_matches_base > 1` → 拒绝发送。
- `target_fingerprint = sha256(account|target_exact|scope)[:16]` 只用于日志关联，不作身份证明。
- 回执必须独立：文本 `confirmed`；贴纸必须 `visible_outgoing` + `sticker_delivered`。

## 5. 回执字段（统一）

`normalize_receipt()` 输出：

```json
{
  "platform": "qq",
  "scope": "group",
  "target": "100000001",
  "kind": "text",
  "accepted": true,
  "delivered": true,
  "confirmed": true,
  "text_delivered": true,
  "visible_outgoing": false,
  "sticker_delivered": null,
  "message_id": "m-42",
  "reason": "accepted",
  "attempt": 1
}
```

- `accepted`：通道接受请求，不等于送达。
- `delivered` / `confirmed`：按 `kind` 证明；贴纸要求 `visible_outgoing` 且 `sticker_delivered=true`。
- `text_delivered`：文本部分是否送达；贴纸未确认时用于“只重试贴纸”，不重发文本。
- `message_id`：可选；仅用于回执/撤回关联。
- `attempt`：从 1 开始；只用于重试日志。

`retry_plan(receipt, max_attempts=3)` 只返回计划：

- 已确认 → 不重试。
- `target_mismatch / wrong_group / wrong_chat / unsafe_text / text_over_budget / duplicate_suppressed` → 永久失败，不重试。
- 瞬态失败 → 重试；贴纸已送达但不可见 → `retry_sticker_only`。
- C 不自动发送；主控在现有发送器执行。

## 6. 重复、撤回、token

- `OutboundDeduplicator` 按完整 payload 的稳定 hash 去重，窗口默认 30 秒；只保存内存，不落盘。
- 重复抑制不替代目标守卫：不同 target 的相同文本不视为重复。
- 撤回先构造 payload，再由主控走现有通道；C 不调用 `delete_msg`。
- `validate_text()` 拒绝 `[CQ:`、URL、`file://`、`base64://`、`sk-...`、`Bearer ...`、`password/api_key/token=` 等敏感值；拒绝即不生成 payload。

## 7. Feature flags

| 环境变量 | 默认 | 启用 | 关闭/回退 |
| --- | --- | --- | --- |
| `DAFEIYU_SOCIAL_STYLE` | `0` | `1` 注入表达约束 | `0` 旧 persona 完全不变 |
| `DAFEIYU_STICKER_RERANK` | `0` | `1` 重排贴纸候选 | `0`/异常 → 现有 `available()` |
| `DAFEIYU_SOCIAL_STYLE_SHADOW` | `1` | `1` 只记录决策 | `0` 才实际注入 |

灰度：shadow → prompt-only → sticker-rerank；env 置 `0` 即回滚。异常必须 `try/except` 走旧路径，不能阻断回复。

## 8. Pi 3B 预算与可选回退

`runtime_budget.py` 的 `PI3B_BUDGET` 只做静态门控，**不是 Pi 实测**：

| 层 | 上限 | 超限行为 |
| --- | --- | --- |
| 语气规划 | `max_style_chars=140`，`max_bubbles=2` | `style=false`，回退旧 persona |
| 贴纸重排 | `max_catalog_items=64` | `sticker=false`，回退 `available()` |
| GIF 候选 | `max_gif_items=16` | `gif=false`，回退静态贴纸/无图 |
| 模块内存 | `module_memory_kb=4096` | 仅门控，不宣称 Pi 实测 |

```python
plan = plan_optional_layers(
    catalog_size=len(catalog),
    gif_candidates=len(gif_candidates),
    requested_max_chars=profile.max_chars,
    requested_bubbles=profile.max_bubbles,
)
if not plan.sticker:
    selection = None          # 或直接走 legacy available()
```

无新常驻服务、无新网络端口、无重量级依赖；只用 Python 标准库。

## 9. 主控验收命令

```powershell
cd <project-root>\开发工作区\2026-11-17-social-expression-c\integrations\social-expression
python -m unittest discover -s tests -p "test_transport_protocol.py"
python -m unittest discover -s tests
python tools\budget_probe.py
```

- 旧 96 项作为基线；新增协议测试单独计数，不合并进旧指标。
- 真实 QQ/微信 E2E、Pi 3B 实测、Swift 端口仍为 **NOT_RUN**。
- 任何生产写、真群发送、重启、合并 main 只由主控执行。
