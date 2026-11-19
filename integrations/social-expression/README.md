# 大肥鱼·社交表达与多模态选择层（会话 C）

本目录是**隔离开发模块**，不接入生产服务，不修改 QQEngine / pi_bridge / tx_guard 的任何现有文件。
它只回答两个问题：

1. **怎么说**：同一轮已决定要发言时，选择句长、语气、是否引用、是否反问、是否适合轻微幽默、最多几个气泡。
2. **配哪张贴纸**：在已有授权素材中按情绪、场景、上下文相关性、冷却与去重排序，交给现有语义审核器选择或放弃。

发言“要不要说”由会话 A 的 `ReplyDecision` 负责；长期记忆压缩由会话 B 负责。本模块只消费二者的最小协议，不写它们的源文件。

## 状态

| 部分 | 状态 |
| --- | --- |
| Python 风格器 / 贴纸层 / 通道构建 | **CODE PASS**（96 项基线；41 条场景 0 偏差） |
| A→C / B→C 字段适配 | **CODE PASS（mock）**（`adapters.py` + 28 项针对性测试） |
| 契约冻结 / 通道协议 | **CODE PASS（离线）**（`a2c-wire-v1` / `b2c-memory-v1` / `c-transport-v1`；新增 31 项协议/预算测试） |
| 微信 Python 接入 | **STAGED**（契约已写，未改生产 `pi_bridge.py`） |
| QQ Swift 接入 | **STAGED / NOT_COMPILED**（无 Swift 工具链；需主会话原生端口） |
| 真实 QQ/微信 E2E | **NOT_RUN**（无生产写权限） |

## 开关与回滚

全部默认关闭，失败回落到既有逻辑：

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `DAFEIYU_SOCIAL_STYLE` | `0` | `1` 时启用场景化表达决策与提示约束 |
| `DAFEIYU_STICKER_RERANK` | `0` | `1` 时启用贴纸相关性排序（失败回落到现有 `available()`） |
| `DAFEIYU_SOCIAL_STYLE_SHADOW` | `0` | `1` 时只记录决策，不改提示/发送；`0` 关闭影子记录 |

回滚：把上述变量设为 `0`，或删除本目录；没有状态迁移、没有新增服务、没有定时任务、没有网络调用。
启用后若抛出任何异常，调用方必须捕获并继续原有路径，不得阻断回复。

## 结构

```text
social-expression/
├── social_expression/
│   ├── models.py          # ChatContext / StyleDecision / StickerCandidate / 协议结构
│   ├── policy.py          # 表驱动场景分类 + 长度/语气/引用/幽默/地板控制
│   ├── stickers.py        # 授权、群范围、相关性、冷却、去重、单小时上限
│   ├── text.py            # 相似度、复读、脱敏、敏感扫描、群梗匹配、记忆摘要
│   ├── prompting.py       # 追加到现有人格提示的紧凑约束块
│   ├── channels.py        # QQ/微信消息构建、目标守卫、发送节流、回执判定
│   ├── adapters.py       # A→C 动作/字段归一；QQMemoryItem→MemoryFact 脱敏适配
│   ├── runtime_budget.py # Pi 3B 静态门控：贴纸/GIF/语气可选回退
│   ├── version.py        # 冻结的契约版本常量
│   ├── mock.py            # mock OneBot / mock 微信 Bridge（仅内存）
│   └── feature_flags.py   # 三个环境开关
├── tests/                 # 旧 96 基线 + 新增 31 项协议/预算测试
├── fixtures/
│   ├── synthetic_style_cases.json
│   ├── style_samples_extra_9.json
│   └── sticker_catalog.json
├── contracts/
│   ├── CONTRACT_session_a_context_style.md
│   ├── CONTRACT_session_b_memory_digest.md
│   ├── CONTRACT_qq_wechat_adapter.md
│   ├── CONTRACT_a_to_c_wire_adapter.md
│   ├── CONTRACT_b_to_c_qq_memory_adapter.md
│   ├── CONTRACT_wechat_target_identity_proposal.md
│   ├── CONTRACT_c_integration_one_pager.md
│   └── style_table_v1.json
├── reports/
│   ├── style_samples_50.md  # 50 例角色风格决策层抽样
│   └── transport_protocol_freeze.md
├── tools/
│   ├── evaluate.py        # 一致性 + 延迟 + 内存
│   ├── export_contract.py # 从代码导出语言中立表
│   ├── budget_probe.py    # 预算门控 + 本机 fallback 自检；Pi NOT_RUN
│   └── sample_style_50.py # 41+9 抽样报告
└── LICENSES_RESEARCH.md
```

## 运行

```powershell
cd <project-root>\开发工作区\2026-11-17-social-expression-c\integrations\social-expression
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p "test_transport_protocol.py" -v
python tools\evaluate.py
python tools\export_contract.py
python tools\budget_probe.py
python tools\sample_style_50.py
```

模块仅使用 Python 标准库；目标运行时兼容 Python 3.9+（生产 Pi 为 3.11）。

## 当前测量

- 41 条合成场景：决策偏差 **0**；回复 32、静默 9；贴纸选中 10 次。
- 8200 次 `decide`：多次采样 p50 **0.038–0.050ms**、p95 **0.071–0.089ms**、max **0.26–0.85ms**（Windows 本机，单次采样会波动）。
- `tracemalloc` 峰值 **4.2KB**（旧基线）；无网络、无模型、无文件。
- 旧 96 项为基线；新增协议/预算测试单独计数：`test_transport_protocol.py` 31 项，全套 127 项。
- `budget_probe.py` 输出中的本机数据只作 fallback 自检，不是 Pi 3B 实测。
- Pi 3B 实机：**NOT_RUN**；不得把 Windows 数值当作生产耗时。

## 许可证

本目录代码为项目原创（随仓库 MIT），无第三方复制。仅参考 MaiBot（GPL-3.0）、MumuBot（GPL-3.0）、AstrBot（AGPL-3.0）的**机制方向**，未复制代码；详见 `LICENSES_RESEARCH.md`。
