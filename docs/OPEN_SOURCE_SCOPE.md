# 公开代码范围与许可边界

本文记录 `Starlevin/dafeiyu-pi-arm64` 中公开代码的许可边界、来源和未纳入内容。根目录的 MIT 许可适用于本仓库原创代码；有独立 `LICENSE` 或素材说明的组件继续适用各自的条款。

## 本次公开的微信集成

- `integrations/wechat-pi/transport/`：大肥鱼的 Linux 微信桥接、可访问性读取与受控投递代码，适用仓库根目录 MIT 许可。
- `integrations/wechat-pi/core/`：基于鲸鲸 / Whale Girl v4.1.0（上游提交 `470fa403039587395a7a14ffa5ec65ec315e2940`）的 AGPL-3.0-only 子项目。保留上游 `LICENSE`、`NOTICE.md`、作者署名与修改说明；联网提供修改版服务时，也要遵守 AGPL 对应源码提供要求。上游来源：[项目仓库](https://github.com/Banan1cee/whale_girl)、[许可证](https://github.com/Banan1cee/whale_girl/blob/main/LICENSE)。
- 本次修改包括：清除私有风格模块引用与部署身份、使用本机环境变量提供目标群/管理员标识/令牌路径、未配置群时默认不进行非 @ 主动接话，并将样例改成合成内容。

## 本次公开的 Pi 统一控制台

- `integrations/pi-console/server/`、`ui/` 与 `model-hub-usage/usage_ledger.py`：大肥鱼控制台和用量记账集成代码，适用仓库根目录 MIT License。
- `integrations/pi-console/server/qrcodegen.py`：Project Nayuki QR Code generator Python 实现，保留文件内完整 MIT 许可声明；来源见 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。
- `integrations/pi-console/server/fixtures/` 及相邻测试：只含合成账号、群组和模型标识。默认监听回环地址、默认不开放局域网服务；本机私有配置、状态目录和密钥不在仓库中。
- 未复制生产配置、实时用量记录、服务单元、证书、可执行文件、归档包、回滚备份或本机截图；也未复制给已安装模型网关打补丁的脚本或来源未确认的上游实现。NapCat 与微信后台仍是外部依赖。

## 未纳入的代码和数据

- SnowLuma：没有复制其源代码。所提供仓库采用非商业源码可见许可，明确限制商业用途，且公开发布修改版/衍生项目须事先取得书面许可；本仓库不合并其代码。[SnowLuma LICENSE](https://github.com/TomyJan/SnowLuma/blob/main/LICENSE)
- NapCatQQ（`VanillaNahida/NapCatQQ`）：没有复制该分支源代码或二进制；项目只把 NapCat 作为外部依赖。该分支许可限制商业用途，并规定修改版不得公开发布，部分使用/分发还要求作者明确许可；本次未取得或主张额外授权。[NapCatQQ LICENSE](https://github.com/VanillaNahida/NapCatQQ/blob/main/LICENSE)
- 不发布账号配置、令牌、个人路径、微信 ID、群名白名单、真实聊天、日志、截图、二维码、备份、个人专属人设文件、来源不明的知识库数据，或用于从客户端进程内提取数据库密钥的实验工具。
- 不发布 Pi 的生产网络地址、服务状态、实时用量快照、证书、可执行文件、部署/回滚包、管理密钥或个人配置。控制台默认使用回环监听和用户私有状态目录；启用局域网或连接后台须由部署者自行配置。
- 许可或来源无法确认的第三方图片、模型素材和数据不因代码开源而获得再分发授权；本次微信集成只收录代码，不带入其运行数据与素材。

## 使用与再分发

微信客户端、腾讯服务、NapCat、系统无障碍组件和本仓库代码分别受各自条款约束。公开代码不代表平台认可，也不授予商标、客户端或账号内容的使用权。再分发 Whale Girl 派生代码时，应同时提供对应源代码，并保留上游许可证、署名和修改记录。

微信集成仍属实验代码。公开版默认没有授权群，运行者必须自行配置、提供必要授权，并在本机私有配置中保存身份和密钥。公开本仓库不会自动公开或上传用户的运行配置。
