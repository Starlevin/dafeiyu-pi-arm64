# 候选开源许可证核验（2026-11-17）

本模块**没有复制**任何上游代码。以下仅记录研究时的真实许可证，供主会话判断“能否移植代码”。

| 项目 | 仓库 | 许可证 | 核验来源 | 结论 |
| --- | --- | --- | --- | --- |
| MaiBot（麦麦） | `Mai-with-u/MaiBot` | **GPL-3.0** | `raw.githubusercontent.com/Mai-with-u/MaiBot/main/LICENSE` 正文为 GNU GPL v3 | 只研究 expression/slang learning、chat timing 的**机制方向**；不复制代码。若移植，GPL-3.0 会要求衍生作品整体 GPL，与本项目 MIT 不兼容，需主会话单独决策。 |
| MumuBot | `SugarMGP/MumuBot` | **GPL-3.0** | `raw.githubusercontent.com/SugarMGP/MumuBot/main/LICENSE` 正文为 GNU GPL v3 | 只研究 group culture 的**数据结构/交互思路**；不复制代码。 |
| AstrBot | `AstrBotDevs/AstrBot` | **AGPL-3.0-or-later** | 官方文档 `docs.astrbot.app` 明确 AGPL-v3；PyPI `AstrBot` 标注 AGPL-3.0-or-later | 只研究插件扩展/平台抽象的**接口分层**；不复制代码。AGPL 的网络服务传染性比 GPL 更强，禁止直接搬入。 |

## 本项目许可

- 仓库根 `LICENSE`：**MIT License**，Copyright (c) 2026 Dafeiyu Contributors。
- 本目录新增代码为原创，随项目 MIT；不包含 GPL/AGPL 代码、素材或提示词全文。

## 采用方式

- **机制级借鉴**：场景表、冷却窗口、上下文相关性排序、插件/平台分层。
- **不采用**：上游代码、模型提示词原文、表情素材、角色设定、数据库 schema。
- **依赖**：仅 Python 标准库；无第三方包、无新服务、无新增二进制。
- 若未来确实要移植上游代码：必须单独开分支，保留 LICENSE/版权头，评估 GPL/AGPL 对整仓分发的影响，并由主会话书面确认；本会话不执行。
