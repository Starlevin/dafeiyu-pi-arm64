# 微信精确目标身份与回执适配提案（审计，不改生产）

版本：`social-style-c1`  
审计对象：`E:\个人\大肥鱼\开发工作区\2026-11-15-微信防错发-隔离复核\production_tx_guard.py`  
状态：**只读审计 + 提案**。本会话未修改 `tx_guard`、`pi_bridge`、`pi_sticker_transport.py`，也未发送任何真实消息。

## 1. 现状

`production_tx_guard.py`：

- `norm_target(text)`（约 90–101 行）会剥掉 `测试群(5)` 的成员数后缀，只留 `测试群`，注释明确“与 pi_bridge.norm_title 同口径”。
- `_signature(sig)`（约 115–125 行）用 `norm_target(title)` 和 `norm_target(row_first_line(selected))` 作为稳定性签名。
- `identity_verdict(sig, target, phase)`（约 128–189 行）：
  - `title = norm_target(sig["title"])`，再与 `target` 比较；
  - `selected = [norm_target(row_first_line(n)) ...]`，再与 `target` 比较；
  - `visible_matches` 必须为 1，`selected_matches` 必须为 1；
  - `group_suffix` 仅作为可选“像群”提示，`REQUIRE_GROUP_HINT` 默认关闭。
- `norm_target` 无法区分 `测试群(4)` 与 `测试群(5)`；`visible_matches` 只统计**当前可见**行，屏幕外/滚动出去的同名会话无法证明不存在（代码注释已承认这一点）。

## 2. 碰撞场景

目标本应是 `测试群(4)`，但 UI 当前选中且可见的是 `测试群(5)`，`测试群(4)` 已滚出屏幕：

- `norm_target("测试群(4)") == norm_target("测试群(5)") == "测试群"`；
- `title`、`selected` 归一后都等于 `target`；
- `visible_matches == 1`（只看到 `(5)`），`selected_matches == 1`；
- `group_suffix` 为真（`(5)` 带后缀），反而可能被当作“像群”放行。

结果：**同名不同成员数的两个群，在只比归一化名字时可能折叠成同一目标**。这是当前 `norm_target` 的已知限制，不是 C 模块引入的。

## 3. 提案：精确目标优先，归一化只做诊断

不要直接把 `norm_target` 作为身份判据；把“精确目标”和“基础名”分开：

```text
target_exact = NFKC(text).strip()      # 保留 (4)/(5)，只统一全角/半角与空白
target_base  = norm_target(text)       # 仅用于日志/兼容旧调用
target_fingerprint = sha256("wechat|account|target_exact|scope")[:16]  # 审计标签，不是身份证明
```

### 3.1 `identity_verdict` 增加精确比较

建议主控在 `tx_guard` 内新增/替换为：

```python
def identity_verdict_exact(sig, target_exact, phase, *, base_name=None):
    # 1. title_raw 必须存在；NFKC 后与 target_exact 完全相等
    # 2. selected 行首行 NFKC 后必须与 target_exact 完全相等
    # 3. visible_matches_exact == 1；visible_matches_base == 1
    # 4. 如果 target_exact 带 "(N)" 后缀，selected 行必须也带同一个 "(N)"
    # 5. 如果 target_exact 不带后缀，而 selected 行带后缀，拒绝（无法证明是同一会话）
    # 6. 任一条件不满足 -> deny，不做“剥后缀后相等”的降级
```

关键点：

- **带后缀的目标**：`测试群(4)` 只能匹配 `测试群(4)`；选中 `测试群(5)` 必须 `target_mismatch`，即使基础名相同。
- **不带后缀的目标**：只允许在 `visible_matches_base == 1` 且没有可见同名行带后缀时放行；否则拒绝。不要靠“唯一可见”猜。
- **`group_suffix` 不是证明**：它只能作为额外 hint；有后缀仍必须 exact match。
- **无法证明屏幕外**：如果 exact title 不可见/不可读，拒绝，而不是退回 `norm_target`。这是“宁可多拦也不误发”。

### 3.2 `visible_matches` 拆分

当前只有一个 `visible_matches` 计数，建议观测层同时输出：

- `visible_matches_exact`：NFKC 后与 `target_exact` 完全相等的可见行数；
- `visible_matches_base`：`norm_target` 后与 `target_base` 相等的可见行数；
- `selected_matches_exact` / `selected_matches_base`。

放行要求：

```text
visible_matches_exact == 1
visible_matches_base == 1
selected_matches_exact == 1
selected_matches_base == 1
```

若 `visible_matches_base > 1`，即使 exact 只有 1，也说明存在同名不同后缀的可见会话；应拒绝或要求人工确认。

### 3.3 目标指纹的用途

`target_fingerprint` 不是 AT-SPI 能证明的身份；它只能做：

- 全链路日志关联（草稿、发送、回执同一目标）；
- 防止配置层把两个不同 exact 目标混用；
- 作为 sidecar 稳定 ID 的占位，未来若 UI 暴露会话 ID/头像哈希，可替换为强身份。

不得把 `target_fingerprint` 单独当作放行条件。

## 4. 回执适配

现有回执语义必须保持独立：

| 对象 | 成功条件 | 不能做什么 |
| --- | --- | --- |
| 文本 | `confirmed` / `visible_outgoing` | 不能把 `accepted` 当送达 |
| 贴纸 | `visible_outgoing` 且确认可见 | 不能用文本回执证明贴纸送达 |
| 媒体 | `before_media` / `before_media_send` 通过 + 可见回执 | 不能只凭提交成功标记成功 |

提案：

- `accepted != delivered`；`confirmed` 只用于文本/操作确认，贴纸必须 `visible_outgoing`。
- 回执记录 `target_exact` 与 `target_fingerprint`，便于事后核对是否发到 `(4)` 还是 `(5)`。
- 文本和贴纸分别记录 `text_receipt` / `sticker_receipt`，不得合并成一个 `ok`。
- 任何 `unconfirmed` 都只影响贴纸，不把文本成功误报为整体失败；反之也不把文本成功当作贴纸成功。

## 5. C 侧只产生 payload 草案

`social_expression/channels.py` 的约束：

- `build_wechat_message(expected_chat, actual_chat, ...)` 只返回 `{"messages":[...]}` 字典；
- `verify_target` 只做 NFKC + 空白归一，**保留成员数后缀**；`测试群(4)` 与 `测试群(5)` 不相等；
- `TargetGuard` 只做最后比对，不调用任意发送 API；
- 模块内没有 `socket` / `requests` / `websocket` / `subprocess` / `urllib` / `http` 导入（新增单测用 AST 扫描锁定）；
- 真正的 UI 身份证明仍由 `tx_guard` 负责，C 不替代 `before_send` / `before_media`。

## 6. 测试与验收

本模块已有：

- `tests/test_channels_mock.py::test_target_guard_normalizes_width_only`
- `tests/test_channels_mock.py::test_wrong_chat_is_rejected`
- `tests/test_adapters.py::test_member_suffix_is_not_collapsed`
- `tests/test_adapters.py::test_channel_builders_only_return_drafts`
- `tests/test_adapters.py::test_module_does_not_import_network_or_process_libraries`

主控接入后应补：

- `identity_verdict_exact` 对 `测试群(4)` vs `测试群(5)` 的拒绝；
- `visible_matches_base=2` 时拒绝；
- 屏幕外同名无法证明时拒绝；
- 文本/贴纸回执分开记录；
- 错群/错聊天在真实 UI 上零发送。

## 7. 不改生产

- 本提案不修改 `production_tx_guard.py`、`production_pi_bridge.py`、`pi_sticker_transport.py`。
- 主控按本提案另开分支实现，先 shadow 记录 `target_exact` / `target_fingerprint` / `visible_matches_base`，再启用 exact 拒绝。
- 在真实 UI 上验收前，C 的 `DAFEIYU_SOCIAL_STYLE` 保持 0 或 shadow。
