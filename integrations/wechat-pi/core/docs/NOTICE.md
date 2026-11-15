# NOTICE · 署名、角色与第三方许可

本文件是 [LICENSE](LICENSE) 的补充说明，用于记录署名要求、角色素材来源和第三方组件许可。若本文件与 LICENSE 冲突，以 LICENSE 中的法律条款为准。

## 1. 项目署名

- 项目名称：**鲸鲸 / Whale Girl**
- 版权：Copyright (c) 2026 鲸鲸 / Whale Girl contributors
- 项目地址：<https://github.com/Banan1cee/whale_girl>
- 许可：**GNU Affero General Public License v3.0 only（AGPL-3.0-only，OSI 认可的开源许可证）**

**共同作者（按首次贡献排序，共同持有本项目著作权）**：

| 署名方 | GitHub | 提交邮箱 | 说明 |
| --- | --- | --- | --- |
| 高晨阳 | [@Banan1cee](https://github.com/Banan1cee) | chrisgao0312@qq.com | 原作者，项目发起人与主要维护者 |
| 周政 | [@xmuzhou](https://github.com/xmuzhou) | 2117286908@qq.com | 共同作者 |
| Muzi0336 | [@Muzi0336](https://github.com/Muzi0336) | 265007223+Muzi0336@users.noreply.github.com | 共同作者 |

以上为三位作者在 Git 提交中使用的邮箱，**作者本人同意公开**，可在这份署名清单中直接引用。
除此之外的联系方式请走项目 Issues，不要从提交记录里翻别人的私人邮箱。

> 署名以 Git 提交记录为准：`Banan1cee` 与「高晨阳」是同一个人；「周政」与 `zhengzhou` 是同一个人，
> 其 GitHub 账号为 [@xmuzhou](https://github.com/xmuzhou)。
> `dsh <dsh@local>` 是 AI 协作工具代提交的身份，**不是**著作权人，不在此列。
> 注意 GitHub 上的 `@zhengzhou` 是同名的第三方账号，与本项目无关，请勿关联。

分享本项目或其修改版本时，请保留 [LICENSE](../LICENSE)、本 `NOTICE.md` 和版权声明，并在 README、关于页、应用内致谢或其它合理位置注明：

> 基于鲸鲸 / Whale Girl（作者 @Banan1cee、@xmuzhou、@Muzi0336，https://github.com/Banan1cee/whale_girl）修改

同时请标明你做了哪些修改。AGPL-3.0 还要求：分发修改版、或让用户通过网络使用修改版时，必须提供完整对应的源代码。

## 2. 图标与角色

- **图标原创角色作者：上善无形。**
- 本项目使用的鲸鲸图标是在该原创角色基础上的**二创作品**。
- 原创角色的版权、角色设定及相关权利归 **上善无形** 所有；本项目仅按授权范围使用并署名。
- 本项目发行包中附带该二创图标，仅限随本项目一起使用；不代表授权他人单独使用、改编或商业化该角色形象。
- 本项目的许可证**不授予**任何第三方对原创角色、角色设定、商标或肖像的权利。二次使用图标或角色素材前，请先取得原权利人的授权。

涉及的文件包括但不限于：

- `launcher/Assets/app.png`
- `launcher/Assets/app.ico`
- `website/images/launcher-icon.png`
- 使用同一角色形象制作的表情包及其它素材

## 3. 第三方组件

以下组件不属于本项目原创部分，分别适用其原有许可证：

| 组件 | 位置 | 许可证 / 说明 |
| --- | --- | --- |
| wechatauto-replica 1.2.9 | `vendor/wechatauto/` | Apache-2.0；许可证原文见 `vendor/wechatauto/dist-info/licenses/LICENSE` |
| wxauto | 上游项目 | Apache-2.0；`wechatauto-replica` 的 4.x 复刻来源 |
| VB-CABLE | 安装包内的官方安装程序 | VB-Audio Donationware；按官方条款使用 |
| Noto Sans SC | `website/fonts/` | SIL Open Font License 1.1；许可证见该目录 |
| Node.js / Python / .NET 运行时 | `runtime/`、安装包 | 各自项目的许可证；随安装包按原条款分发 |

如果你再分发本项目，请同时保留上述第三方组件的许可证和版权声明。

## 4. 共同作者与许可一致性

本项目由三位共同作者按 AGPL-3.0-only 对外发布，作如下内部约定（便于协作，不构成对第三方的额外限制）：

- **贡献即以 AGPL-3.0-only 授权**：向本项目提交代码，视为同意该贡献以 AGPL-3.0-only 分发，无需另签 CLA。
- **许可不得单方变更**：项目的对外许可证只有一份，即 `LICENSE`。任何作者都不得单方面把项目整体改为闭源或其他许可证；如需变更，须三位作者全部书面同意。
- **商业授权需全体同意**：“商业使用”一节提到的闭源商业授权，须三位作者一致同意方可授予。
- **作者身份以 Git 记录为准**：署名清单变更（新增或移除作者）同样需要三位作者同意，并同步更新 `LICENSE` 头部与本节。

本节只约束项目内部的协作方式，不改变任何人对已发布版本依 AGPL-3.0 享有的权利。

## 5. 商业使用

AGPL-3.0 允许商业使用，不需要额外交费。但如果你分发修改版，或让用户通过网络使用修改版，必须按 AGPL-3.0 提供完整对应的源代码。

如果你需要不受 AGPL 源码开放义务约束的闭源商业授权，可以联系维护者另行协商；这不是 AGPL 的要求，也不影响任何人依据 AGPL 继续使用本项目。

联系方式：<https://github.com/Banan1cee/whale_girl/issues>

请勿在公开 Issue 中发送 API Key、聊天记录或其他敏感信息。
