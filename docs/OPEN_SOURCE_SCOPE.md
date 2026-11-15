# 公开代码范围与许可边界

本文记录 `Starlevin/dafeiyu-pi-arm64` 中公开代码的许可边界、来源和未纳入内容。根目录的 MIT 许可适用于本仓库原创代码；有独立 `LICENSE` 或素材说明的组件继续适用各自的条款。

## 本次公开的微信集成

- `integrations/wechat-pi/transport/`：大肥鱼的 Linux 微信桥接、可访问性读取与受控投递代码，适用仓库根目录 MIT 许可。
- `integrations/wechat-pi/core/`：基于鲸鲸 / Whale Girl v4.1.0（上游提交 `470fa403039587395a7a14ffa5ec65ec315e2940`）的 AGPL-3.0-only 子项目。保留上游 `LICENSE`、`NOTICE.md`、作者署名与修改说明；联网提供修改版服务时，也要遵守 AGPL 对应源码提供要求。上游来源：[项目仓库](https://github.com/Banan1cee/whale_girl)、[许可证](https://github.com/Banan1cee/whale_girl/blob/main/LICENSE)。
- 本次修改包括：清除私有风格模块引用与部署身份、使用本机环境变量提供目标群/管理员标识/令牌路径、未配置群时默认不进行非 @ 主动接话，并将样例改成合成内容。

## 未纳入的代码和数据

- SnowLuma：没有复制其源代码。所提供仓库的许可为非商业源代码许可，限制商业使用及修改版公开分发，故不将其代码合并进本仓库。[SnowLuma LICENSE](https://github.com/TomyJan/SnowLuma/blob/main/LICENSE)
- NapCatQQ（`VanillaNahida/NapCatQQ`）：没有复制该分支源代码或二进制；项目把 NapCat 作为外部依赖。该分支许可证对商业使用和修改版分发设有额外限制。[NapCatQQ LICENSE](https://github.com/VanillaNahida/NapCatQQ/blob/main/LICENSE)
- 不发布账号配置、令牌、个人路径、微信 ID、群名白名单、真实聊天、日志、截图、二维码、备份、个人专属人设文件、来源不明的知识库数据，或用于从客户端进程内提取数据库密钥的实验工具。
- 许可或来源无法确认的第三方图片、模型素材和数据不因代码开源而获得再分发授权；本次微信集成只收录代码，不带入其运行数据与素材。

## 使用与再分发

微信客户端、腾讯服务、NapCat、系统无障碍组件和本仓库代码分别受各自条款约束。公开代码不代表平台认可，也不授予商标、客户端或账号内容的使用权。再分发 Whale Girl 派生代码时，应同时提供对应源代码，并保留上游许可证、署名和修改记录。

微信集成仍属实验代码。公开版默认没有授权群，运行者必须自行配置、提供必要授权，并在本机私有配置中保存身份和密钥。公开本仓库不会自动公开或上传用户的运行配置。
