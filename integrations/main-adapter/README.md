# 主控 R2 单一适配器（A/B → C）

- 分支：`feat/main-adapter-r2-20261117`
- 基线：C `dbec61d9be158795a45e73afb95f69390bc665ee`（`feat/social-expression-c-20261117`）
- 隔离 worktree：`E:\个人\大肥鱼\开发工作区\2026-11-17-main-adapter-r2`
- 状态：`CODE PASS`（mock），`STAGED`（未合并、未接生产），真实 E2E `NOT_RUN`

## 交付

- `social_expression/adapters.py`：A→C/B→C 唯一转换层，严格验证、失败关闭；主控补入 `from_a_decision` 与 `memory_facts`。
- `social_expression/policy.py`：A 明确 `SPEAK` 后 C 不再用低 relevance 反向否决；仍保留 quiet hours、重复 floor、不可读媒体等硬约束。
- `tests/test_main_adapter_e2e.py`：30 例精确群号 mock 发送、错误群号拒绝、跨群记忆过滤、TTL/过期/删除、citable 上限 3。

## 契约

- A：只接受 `SPEAK/WAIT/SKIP`（大小写归一）；`addressed` 由主控根据 private、已验证 @ self、已核验 reply_to_bot、invited 派生，不直接信任输入字段。`reply_to_bot` 未核验时不得当作必答。`ttl_seconds` 超时转 `wait`。
- B：每条事实必须带 scope、subject、expiresAt；scope 不匹配、subject 不匹配、过期、invalidated 全部丢弃。`subject_hash` 只输出哈希，不输出原始 subject/群号。按 C 契约，只有 `preference/task` 且带 sourceIDs 才 `citable`；`fact/event/episode/legacy` 保留但不注入。
- C：最多 3 条 fact；只生成 payload 草案，发送仍由生产 `pi_bridge`/`tx_guard` 执行；本适配器不打开 socket、不持令牌。

## 验证

```powershell
cd "E:\个人\大肥鱼\开发工作区\2026-11-17-main-adapter-r2\integrations\social-expression"
python -m unittest discover -s tests -v
python -m unittest tests.test_main_adapter_e2e -v
```

- 全量：`Ran 113 tests ... OK`（C 最新 96 + 主控 17）
- 主控 mock 30 例：`sent=30 rejected=0 p50_ms=0.125 p95_ms=0.228`
- 伪群号：`send_group_msg` 被 MockOneBot 拒绝（`wrong_group`），builder 在发送前抛 `OutboundError`；错误群 `accepted=0`
- 跨群记忆：12 个 scope 中只有当前 scope 的 1 条进入，输出无原 subject/scope；上限 3
- 证据 JSON：`evidence.json`

## 未完成

- 真实 QQ/微信入站 E2E：`NOT_RUN`（没有独立真实入站；不得伪造）
- C 生产发送接线、A/B Swift 实现对接：`NOT_RUN`
- 合并 main / 部署 / 灰度：未授权
