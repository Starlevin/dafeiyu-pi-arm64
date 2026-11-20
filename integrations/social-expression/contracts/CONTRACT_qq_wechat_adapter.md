# 契约 C：QQ / 微信通道接入（主会话执行）

版本：`social-style-c1` / 通道 `c-transport-v1`  
冻结单页：`CONTRACT_c_integration_one_pager.md`（目标 ID、回执字段、重复/撤回/重试、预算回退以此页为准）。  
状态：**接口提案 + 可运行 Python 模块**。本会话没有生产写权限，未修改 `QQEngine.swift`、`pi_bridge.py`、`tx_guard.py`。

## 0. 交付状态

| 部分 | 状态 | 证据 |
| --- | --- | --- |
| Python 风格器 / 贴纸层 / 通道构建 | **CODE PASS** | 96 项单测；41 条场景 0 偏差；mock 全链路 |
| 微信 Python 侧接入 | **STAGED** | 本契约；未写生产 `pi_bridge.py` |
| QQ Swift 侧接入 | **STAGED / NOT_COMPILED** | 本契约 + `style_table_v1.json`；无 Swift 工具链，未编译 |
| 真实 QQ/微信 E2E | **NOT_RUN** | 禁止生产写；等待主会话灰度 |

## 1. 通用开关（默认全关）

```text
DAFEIYU_SOCIAL_STYLE=0         # 1 才启用；0 时旧路径完全不变
DAFEIYU_STICKER_RERANK=0       # 1 才启用贴纸重排；失败回落 available()
DAFEIYU_SOCIAL_STYLE_SHADOW=1  # 1 只记录决策，不改提示/发送
```

失败策略：任何异常都必须 `try/except` 后继续原有路径；记录 `social_style_error` 计数，不得阻断回复。

## 2. 微信 Python 接入（推荐最小改动）

### 2.1 输入组装

在现有群/私聊策略判定**之后**、组装人格/模型提示**之前**：

```python
from social_expression import (
    ChatContext, ChatMessage, BotReply, StickerHistory, StyleProfile,
    decide, style_prompt_constraints, is_too_similar,
)

profile = StyleProfile(
    platform="wechat",
    max_chars=70 if scope == "group" else 140,
    max_bubbles=2,
    version="social-style-c1",
)
ctx = ChatContext(
    platform="wechat", scope=scope, conversation_id=chat_title,
    self_id=self_id, sender_id=sender_id, sender_role=role, text=text,
    profile=profile,
    mentioned_bot=mentioned, reply_to_bot=reply_to_bot, quoted_bot=quoted_bot,
    has_media=bool(images), media_readable=vision_ok,
    ambient_relevance=bridge_relevance,          # 0..1，现有桥接侧给出
    upstream=session_a_decision,                 # 可选；A 未部署则 None
    recent_messages=recent_messages,             # 最近 40 条，seconds_ago；不含当前这条入站
    bot_replies=bot_replies,                     # 最近 40 条，seconds_ago；不含当前这轮草稿
    sticker_history=sticker_history,
    local_hour=local_hour,
    sensitive_flags=scan_sensitive(text),
)
decision = decide(ctx)
```

注意：`seconds_ago` 由调用方换算；C 不读时钟。`conversation_id` 用经过 `tx_guard` 身份证明的当前会话标题。

### 2.2 提示注入

```python
base_prompt = existing_persona_prompt(...)
style_block = style_prompt_constraints(ctx, decision)
if style_block:
    base_prompt = base_prompt + "\n\n" + style_block
```

- 只追加，不替换人格；不新增身份、权限、事实。
- `decision.action == "silence"` 时直接走现有“不回复”路径，不调用模型。
- 群聊 `max_bubbles=1`；私聊最多 2；实际分条仍由现有发送器控制。

### 2.3 生成后复读检查

```python
draft = model_output
if is_too_similar(draft, [reply.text for reply in bot_replies[-3:]], threshold=0.72):
    draft = regenerate_with(style_block + "\n必须换开头和句式，不要复用最近句子。")
    if is_too_similar(draft, [reply.text for reply in bot_replies[-3:]], threshold=0.72):
        if scope == "group":
            return None            # 宁可不说，也不复读
        draft = shortest_safe_answer  # 私聊保底一句，仍受事实/安全约束
```

`is_too_similar` 是字符二元组 Jaccard，短文本退化为字符集合比较；不是语义相似度。

### 2.4 贴纸

```python
selection = select_sticker(ctx, decision, catalog)  # 只在 DAFEIYU_STICKER_RERANK=1 时
if selection:
    candidate_ids = [selection.item_id] + [item.id for item in selection.alternatives]
else:
    candidate_ids = legacy_library_available_ids
```

- `catalog` 必须由现有素材清单映射：`origin=bundled/original/licensed`、`license/author`、`group_ok/public_sharing`、`sha256`。
- 贴纸层只返回 id 和元数据，不返回路径；路径由现有 `dispatch_sticker` 解析。
- 必须保留 `tx_guard.before_media()` 和 `process_sticker()` 的现有确认；`visible_outgoing/confirmed` 才算送达。
- 群成员头像/照片/转发来源不明素材不得进入 catalog（硬过滤已在模块内）。

### 2.5 发送

`build_wechat_message()` 可用于 mock/单测；生产仍走现有 `pi_bridge` 发送路径。若使用构建结果：

- `chat` 必须与 `tx_guard` 证明的标题完全一致（宽度归一化后）；`测试群(4)` 与 `测试群(5)` 不可互认。
- `mention` 只在真实 @ 时传入；正文不得含链接/CQ/本地路径。
- 文本长度 <= `decision.max_chars + 8`；超限拒绝发送，不静默截断。

## 3. QQ Swift 接入（需要原生端口，本会话未编译）

QQ 核心是 Swift，不能 import Python。主会话二选一：

1. **原生 Swift 端口**：按 `contracts/style_table_v1.json` 实现 `decide` + 贴纸评分；只依赖 Foundation，禁止网络/文件。
2. **保持 Python 为参考实现**：先只在微信启用，QQ 等 Swift 端口完成后再开开关。

### 3.1 Swift 端口最小清单

- `ChatContext` 字段：`platform/scope/conversationID/selfID/senderID/senderRole/text/profile/mentionedBot/replyToBot/quotedBot/hasMedia/mediaReadable/isForward/ambientRelevance/upstream/recentMessages/botReplies/stickerHistory/groupMemes/sensitiveFlags/localHour/voiceRequested`。
- 场景表：`style_table_v1.json["scenes"]`；顺序与 `policy.classify_scene` 一致，先危机/凭据/敏感，再求助/情绪/冲突/庆祝/睡眠/招呼/媒体/游戏/斗嘴/转发/详细/提问/兜底。
- 阈值：`ambient_relevance=0.70`、`quiet=0..8`、`floor 120s/30s/300s/3`、重复相似度 0.72。
- 文本相似度：NFKC -> 小写 -> 去标点/空白 -> 字符 bigram Jaccard；短文本退化为字符集合。
- 贴纸：只吃已授权 catalog；硬过滤 `member_photo/member_avatar/forwarded_unknown`；群聊要求 `group_ok && public_sharing`；冷却 180s、至少 3 条非贴纸回复、单贴 24h、单小时 2 张、同人同贴 30min 不镜像。
- 输出：`StyleDecision` 字段名与 Python 完全一致，便于日志/回放。

### 3.2 QQEngine 接入点（基线 `3675e58`）

- `QQEngine.swift` 约 840-930 行：群参与/私聊入站判定后，构造 `ChatContext`；保留现有 `persona.rebuff`、`sends.allowance`、`config.effective*` 逻辑。
- 约 923 行 `rule.prompt = persona.prompt(notes: config.ai.prompt)` 之后：追加 `styleBlock`，不要覆盖原 prompt。
- 约 1107 行 `stickerOptions = stickerLibrary.available(...)`：开启 `DAFEIYU_STICKER_RERANK` 时，用 C 的评分把候选重排为 `[top, alternatives...]`；关闭/异常时原样使用 `available()`。
- 约 1113-1160 行模型输出后：用 `isTooSimilar` 检查；命中则带约束重生成 1 次；群聊仍命中则放弃发送，私聊用最短安全答复。
- `QQPolicy.sendAction(target:text:image:)` 仍是发送出口；C 不拼 CQ 码、不写文件路径、不改 `QQTarget`。
- 真实 @：`QQIncoming.mentionsSelf` 为真时，发送段仍由现有逻辑加 `at`；C 的 `build_qq_group_message` 只是 mock 协议，不替代 `sendAction`。

### 3.3 Swift 端硬约束

- `QQPersona.groupMaxChars=60`、`maxBubbles=1`：C 的 `max_chars` 不得超过；风格块只做“更短”，不做“更长”。
- 不把 `meme_term` 写进长期记忆；只在本轮 prompt 出现一次。
- 危机/医疗/地址/身份/财务/冲突轮次：`sticker=none/humor=none/quote=none/memory_allowed=false`，不得被模型自行放宽。
- 图片未读取：`mediaReadable=false` 时不得让模型猜图；群聊直接静默，私聊只问一句“没看清，是哪张”。

## 4. 灰度与回滚

1. 先 `DAFEIYU_SOCIAL_STYLE_SHADOW=1`、`DAFEIYU_SOCIAL_STYLE=1`：只记录决策，人工抽查 100 条群聊/私聊；确认无语义误判。
2. 再 `SHADOW=0`、`STYLE=1`：只注入 `style_prompt_constraints`，不改贴纸。
3. 最后 `STICKER_RERANK=1`：只重排候选，仍由现有语义审核器选/弃。
4. 任一步异常：对应 env 置 0；无状态迁移、无新增进程、无定时任务。
5. 禁止在未验收前改主分支/生产服务；真实 4 群/2 私聊 E2E 由主会话执行。

## 5. 主会话验收清单

- [ ] `python -m unittest discover -s tests` 全绿（旧 96 项基线；新增协议测试单独计数）。
- [ ] 微信 mock：准确群名、@对象、贴纸回执、图片 fallback、节流、错发守卫。
- [ ] QQ Swift 端口编译通过（本会话 NOT_COMPILED）。
- [ ] QQ mock/真实 OneBot：准确群号、@QQ、贴纸 `visible_outgoing` 回执。
- [ ] 4 群/2 私聊灰度：无错群、无复读、无连续抢话、无未授权贴纸。
- [ ] 回滚演练：env 置 0 后 60 秒内恢复旧行为。
