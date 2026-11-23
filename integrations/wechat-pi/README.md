# WeChat on Linux（实验集成）

本目录包含 Whale Girl core 的大肥鱼适配层，以及通过 Linux 桌面无障碍接口读取会话、核验目标并投递回复的桥接代码。集成仍在测试中，不能视为已通过 Pi 部署或微信真实消息验收。

## 许可与来源

- `core/` 基于 [Whale Girl v4.1.0](https://github.com/Banan1cee/whale_girl)，适用 **AGPL-3.0-only**。上游许可证、共同作者署名和 NOTICE 保存在 `core/LICENSE`、`core/docs/NOTICE.md`。修改包括微信接入、主动接话闸门、群管理及本仓库适配。分发或通过网络提供修改版时，需按 AGPL 提供对应源代码并保留上游声明。
- `transport/` 是本仓库的微信 Linux 适配代码，适用仓库根目录 MIT 许可。
- 许可边界、未纳入的仓库和数据见[公开代码范围](../../docs/OPEN_SOURCE_SCOPE.md)。

## 本机配置

不要把 `.env`、服务文件、配置 JSON、模型密钥、管理员标识、群名或聊天资料提交到 Git。以下环境变量由部署者在本机私有配置中设置；示例值应替换为自己的值。

| 环境变量 | 用途 | 默认行为 |
| --- | --- | --- |
| `DAFEIYU_WECHAT_HOME` | 私有运行目录 | `~/.local/share/dafeiyu-wechat` |
| `DAFEIYU_WECHAT_CONFIG_HOME` | 私有配置目录 | `~/.config/dafeiyu-wechat` |
| `DAFEIYU_WECHAT_STATE_HOME` | 私有状态目录 | `$XDG_STATE_HOME/dafeiyu-wechat` 或 `~/.local/state/dafeiyu-wechat` |
| `DAFEIYU_WECHAT_TOKEN_FILE` | 网关令牌 JSON 文件 | 配置目录下的 `config.json` |
| `DAFEIYU_WECHAT_CORE_HOME` | core 数据目录 | 运行目录下的 `core` |
| `DAFEIYU_WECHAT_STAGING_HOME` | 媒体暂存目录 | 运行目录下的 `staging` |
| `DAFEIYU_WECHAT_CLIENT_RUNTIME` | 微信桌面会话运行目录 | 运行目录下的 `client-session/runtime` |
| `DAFEIYU_WECHAT_SPECIAL_GROUP` | 可选媒体/界面特例的目标群 | 空；相关路径关闭 |
| `DAFEIYU_WECHAT_STICKER_GROUP` | 可选贴纸发送白名单群 | 空；贴纸发送关闭 |
| `DAFEIYU_WECHAT_STICKERS` | 显式启用贴纸发送 | 关闭 |
| `DAFEIYU_WECHAT_AUTO_REPLY_GROUP` | 唯一的非 @ 主动接话群 | 空；主动接话关闭 |
| `DAFEIYU_WECHAT_AUTO_REPLY_ENABLED` | 桥接层非 @ 主动接话开关 | 关闭；须同时配置 core 白名单 |
| `DAFEIYU_WECHAT_ADMIN_LOCAL_ID` | 私聊管理员的本地稳定标识 | 空；配置 `--admin-chat` 时拒绝启动 |
| `DAFEIYU_WECHAT_JUDGE_KEY_FILE` | 主动接话评分器密钥文件 | 配置目录下的本机密钥文件 |
| `DAFEIYU_WECHAT_JUDGE_KEY` | 主动接话评分器的临时密钥注入 | 空；密钥不得记录或提交 |

群白名单还必须在 core 的 `group.proactiveJudge.groups` 中显式配置；只设置环境变量不会授权 core。测试时使用专用测试账号、合成聊天资料和可撤销的最小权限配置。

## Swift 群聊上下文

对于已路由至 Swift 的微信文本群聊，在 Swift 服务的私有配置中设置 `DAFEIYU_WECHAT_SWIFT_MEMORY=1`，并设置持久化 `DAFEIYU_DATA_DIR`、`DAFEIYU_UNIFIED_ALLOWED_ACCOUNTS` 和 `DAFEIYU_UNIFIED_ALLOWED_CHATS`。同时更新 Swift 可执行文件与微信桥接。

桥接核验发出的消息气泡后才确认该轮上下文。重启后，Swift 按原账号和群召回最近的已确认轮次。记忆保存在权限为 `0600` 的本机 `unified-pilot-journal.json`，备份时按私有聊天数据处理。关闭环境开关并重启 Swift 服务即可停止使用这项记忆。

在已启用的 Swift 文本群聊中，可以原生 @ 机器人后使用 `记住：集合地点＝图书馆西门`。`记住偏好：名称＝内容` 和 `记住事项：名称＝内容` 分别保存偏好和事项。同名记录可以再次提交以更正。回复获得送达确认后，条目写入 `DAFEIYU_DATA_DIR/wechat-group-memory` 内对应的私有群目录。之后用正常问句提到记录名称，即可从原群检索。

## 桌面交互与当前验收

桥接代码使用 AT-SPI 读取 Linux 微信界面，并通过输入适配层完成必要的界面操作。发布这份源代码时没有启动桌面自动化、没有点击或键入、没有发送微信消息，也没有部署到 Pi。当前公开快照的 Pi 编译、真实图片识别、管理员命令和实际微信回复均未验收；进度和已知限制见[开发记录](../../docs/DEVELOPMENT_STATUS_2026-11-15.md)。
