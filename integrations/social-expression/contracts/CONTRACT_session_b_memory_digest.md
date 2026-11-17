# 契约 B：会话 B（记忆压缩）↔ 会话 C（表达风格）

版本：`social-style-c1`  
状态：提案。C 不读写 B 的记忆库，不持久化任何原始聊天。

## 1. 数据流

```
会话 B：原始聊天 -> 脱敏/摘要 -> MemoryFact（可引用事实）
                                  -> GroupMemeDigest（群内梗白名单）
                                              |
会话 C：只读上述结构 -> StyleDecision（长度/语气/梗/贴纸）
                                  -> StyleMemoryDigest（弱风格信号，不可引用）
```

C 不持有数据库连接，不写 B 的源文件，不调用 B 的写入 API。B 可以把 C 的 `StyleMemoryDigest` 当作**弱偏好信号**，但不能升级为个人事实。

## 2. B → C：可引用事实

字段与 `social_expression.models.MemoryFact` 一致：

```json
{
  "subject_hash": "a1b2c3d4e5f6",
  "fact": "偏好简短回复，不喜欢被追问私人住址",
  "confidence": 0.82,
  "valid_for_seconds": 2592000,
  "citable": true
}
```

硬约束：

- `subject_hash` 是 B 用固定盐对 `platform|scope|conversation|sender` 做的不可逆摘要；C 不接收原始 QQ/微信 ID。
- `fact` 已由 B 脱敏，<=160 字；不得包含手机号、住址、病历、金额、身份证、群成员照片描述。
- `citable=false` 的事实 C 一律不注入提示；C 最多读 3 条、`confidence>=0.5`、未过期。
- C 不把事实写回任何文件；只在当前轮提示中使用，轮次结束即丢弃。

## 3. B → C：群内梗白名单

字段与 `social_expression.models.GroupMeme` 一致：

```json
{
  "term": "寄",
  "meaning": "输了、完了",
  "group_id": "in-process-only",
  "group_key": "g_9f2c1a",
  "safe": true,
  "sensitive": false,
  "expires_in_seconds": 86400
}
```

硬约束：

- `group_key` 是 B 生成的群级哈希（<=32 字符）；C 优先用 `group_key` 匹配，避免持久化真实群号。
- `group_id` 仅用于当前进程内的精确匹配，C 不落盘；生产适配器可只传 `group_key`。
- 只允许 `safe=true`、`sensitive=false`、未过期的梗；每轮最多用 1 个；只在当前群、当前语境自然相关时使用。
- 私聊、跨群、敏感话题（医疗/地址/身份/财务/危机/性/未成年）一律不用梗。
- C 不保存梗的原始出处消息，不解释“这是你们群里的梗”，不把梗带到群外。

## 4. C → B：弱风格信号

字段与 `social_expression.text.style_memory_digest` 一致：

```json
{
  "subject": "a1b2c3d4e5f6",
  "platform": "qq",
  "scope": "group",
  "tags": ["banter", "playful", "short", "no_sticker"],
  "citable": false,
  "raw_text": null,
  "confidence": 0.8,
  "profile_version": "social-style-c1"
}
```

- 永远 `citable=false`、`raw_text=null`。
- 只表达“这轮的表达形状”，不含用户原话、账号、群号、图片描述。
- B 若存储，只能作为“该会话偏好短答/少梗/少贴纸”的弱信号，TTL 建议 <=7 天；不得与个人事实合并。

## 5. 红线

- 不默认保存私聊原文；C 没有原文存储能力。
- 不把群成员头像/照片/聊天图片当作贴纸素材；贴纸层硬拒绝 `member_photo/member_avatar/forwarded_unknown`。
- 不跨群复用梗；`group_key` 不匹配即放弃。
- 不在危机/医疗/地址/财务/身份/冲突轮次使用幽默、贴纸、引用或梗。
- 两个会话不直接写同一源文件；接口变更先写契约提案，由主会话合并。
