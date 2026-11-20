# C 契约冻结与离线通道协议测试日志

日期：2026-11-17  
分支：`feat/social-expression-c-20261117`  
代码提交：`aa093b9`（在 `dbec61d` 之上）  
模块目录：`<project-root>\开发工作区\2026-11-17-social-expression-c\integrations\social-expression`  
状态：**CODE PASS（离线）/ STAGED（生产接入）/ E2E NOT_RUN**

## 1. 冻结版本

| 契约 | 版本常量 | 值 |
| --- | --- | --- |
| 总体风格 | `STYLE_CONTRACT_VERSION` | `social-style-c1` |
| A→C | `A_TO_C_VERSION` | `a2c-wire-v1` |
| B→C | `B_TO_C_VERSION` | `b2c-memory-v1` |
| 通道协议 | `CHANNEL_PROTOCOL_VERSION` | `c-transport-v1` |
| 冻结日期 | `CONTRACT_FROZEN_AT` | `2026-11-17` |

单页集成指南：`contracts/CONTRACT_c_integration_one_pager.md`。

A/B 线上 payload 的 `contract_version` 可省略；一旦出现且不匹配，A 侧回落 `WAIT`，B 侧丢弃事实（`version_mismatch`）。

## 2. 新增测试（单独计数）

旧基线：`dbec61d` 的 96 项（不重算、不合并）。  
本次新增：`tests/test_transport_protocol.py` **31 项**。  
全套：**127 项**。

分组：

- `ContractFreezeTests` 6 项：版本常量、`WAIT/SKIP` 不升级、TTL 过期、版本化 payload、版本不匹配 fail-closed、跨群事实不注入。
- `QQProtocolTests` 12 项：reply+at 顺序、私聊 exact user、重复抑制/窗口、不同目标不误判、撤回 payload/mock、撤回错误目标/ID、accepted≠delivered、贴纸可见性、瞬态重试、token 拒绝、mock 失败后重试、只返回草案。
- `WeChatProtocolTests` 8 项：mention+reply、重复、撤回、撤回错误目标、贴纸可见回执、贴纸未确认只重试贴纸、token、只返回草案。
- `BudgetFallbackTests` 5 项：预算内全开、catalog 超限回退、GIF 超限回退、风格超限回退、负数拒绝。

## 3. 验证命令与结果

```powershell
cd <project-root>\开发工作区\2026-11-17-social-expression-c\integrations\social-expression
python -m unittest discover -s tests
python -m unittest discover -s tests -p "test_transport_protocol.py"
python tools\budget_probe.py
```

结果（2026-11-17 本机新鲜运行）：

```text
Ran 127 tests in 0.058s
OK

Ran 31 tests in 0.007s
OK
```

## 4. fail-closed 证据点

- **A=WAIT/SKIP**：`decide()` 直接 `silence`，点名/私聊/高相关都不翻案；测试覆盖。
- **TTL**：`ttl_seconds<=0` 或 `age > ttl` 时 `upstream_allows_send()` 为假；过期 `SPEAK` 不发送。
- **A 契约错误**：`from_session_a()` 统一回落 `WAIT`，reason 前缀 `upstream_contract_error:`；版本不匹配同样回落。
- **B scope 不一致**：`adapt_qq_memory_item()` 返回 `None`；跨群 `OWNER` 不进入提示。
- **B 过期/删除/错误人称/缺 expiry**：均返回 `None`，批量只保留合法项。
- **通道**：`build_*` 只返回 dict；没有 socket/requests/subprocess；目标不匹配在构造阶段即抛 `OutboundError`。
- **回执**：`accepted` 不等于 `delivered`；贴纸 `confirmed` 必须 `visible_outgoing=true` 且 `sticker_delivered=true`。
- **重复**：完整 payload 稳定 hash，内存窗口去重；不同 target 的相同文本不误杀。
- **撤回**：只构造 `delete_msg` / `recall_message` 草案，由主控现有通道执行。
- **token**：`Bearer ...`、`api_key=...`、`password: ...`、`sk-...` 在生成 payload 前拒绝。

## 5. Pi 3B 预算门控

`runtime_budget.py` / `PI3B_BUDGET` 是静态门控，不是实测：

- 语气：`max_style_chars=140`、`max_bubbles=2`；超限 `style=false`，回退旧 persona。
- 贴纸：`max_catalog_items=64`；超限 `sticker=false`，回退现有 `available()`。
- GIF：`max_gif_items=16`；超限 `gif=false`，回退静态贴纸/无图。
- 模块内存：`module_memory_kb=4096`（门控值，不宣称 Pi 实测）。
- 无新增常驻服务、无新端口、无重量级依赖；`runtime_budget.py` 仅标准库。

本机 `budget_probe.py` 输出：

```json
{
  "environment": "Windows local",
  "pi3b": "NOT_RUN",
  "local_only": {
    "cases_per_loop": 41,
    "loops": 100,
    "decisions": 4100,
    "wall_seconds": 1.107,
    "p50_ms": 0.1544,
    "p95_ms": 0.7355,
    "max_ms": 2.5669,
    "peak_kb": 198.1
  }
}
```

**Pi 3B CPU/内存 = NOT_RUN**；以上数字仅证明 fallback 逻辑可执行，不得作为生产预算结论。

## 6. 生产接入状态

- `production_tx_guard.py`、`production_pi_bridge.py`、`QQEngine.swift` 未修改。
- 真实 QQ 4 群/2 私聊、微信群/私聊发送、撤回、贴纸回执、E2E 全部 **NOT_RUN**。
- 主控接入时只消费单页指南；C 不自行发送、不重启、不合并 main。

## 7. 回滚

- `DAFEIYU_SOCIAL_STYLE=0`、`DAFEIYU_STICKER_RERANK=0` 即回退。
- `git revert aa093b9` 撤销本提交；`dbec61d` 基线保留。
- 无数据库迁移、无新增进程、无定时任务。
