# Raspberry Pi 3B · 大肥鱼 P0 服务托管修复

**变更日期：2026-11-09（Asia/Shanghai）**

本目录提供新的前台启动器、systemd 用户单元、默认只读的恢复观测器和预检脚本。修复目标是避免旧版 Type=forking + 启动后反复查询控制端口导致的超时、双实例和重启循环。

## 核心改动

- 使用 systemd `Type=simple` 管理唯一的前台大肥鱼进程，取消 PIDFile 和阻塞式就绪轮询。
- 新启动器在启动前检查真实二进制路径与运行实例，使用 flock 防止重复启动；有冲突时以退出码 78 拒绝运行，**不会杀掉已有进程**。
- 从原有部署脚本经 `prepare-runtime-profile.py` 生成私有运行配置（0600），保留原来的模型网关、密钥文件*路径引用*、OneBot 配置、回复白名单和自动恢复参数。工具不会读取密钥文件内容；私有配置不得提交仓库。
- 每次成功接管新一代进程前，私有日志重新建立并保持 0600，避免旧控制 URL/授权信息混入新进程状态。
- 恢复观测器默认仅探测和记录状态，不与 systemd 竞争重启引擎；不主动重启 QQ/NapCat。新版本仍需独立验证断网与重启场景。

## 安全部署条件

1. 先在树莓派创建本机私有备份，包括原 systemd 单元、运行脚本和 QQ 登录态。
2. 使用 `prepare-runtime-profile.py --check-only` 验证旧启动配置，然后生成仅限本机使用的私有配置文件。
3. 在独立目录运行 `bash -n`、`systemd-analyze --user verify` 和 `python3 scripts/test-pi-systemd-p0.py`，确认所有脚本是 Unix LF 换行格式。
4. 不允许直接对仍有旧机器人实例的环境执行 `systemctl --user start dafeiyu.service`。必须在维护窗口内暂停恢复定时器、确认单实例、优雅停止旧引擎，然后启动新单元，**QQ/NapCat 不重启**。
5. 切换后检查：`dafeiyu.service` active/running、唯一主进程属于该 unit、OneBot 3001 监听、`enable-target.py --status` 返回 connected/running、恢复 timer active。失败需按私有备份回滚。

## 已实测结果（2026-11-09）

- Windows 与真实 Pi 环境均通过 **34/34** 个合成测试。
- Pi 上通过 Bash 语法检查、私有配置生成检查、systemd 单元检查、启动器拒绝双实例（退出 78 且原进程不变），以及带模拟程序的参数传递检查。
- 已在受控维护窗口切换生产大肥鱼引擎，**没有重启 QQ/NapCat**。独立复查显示 `dafeiyu.service` active/running、1 个主进程归属该 unit、`NRestarts=0`、`Linger=yes`、恢复 timer active、控制接口 `connected=True` 和 `running=True`。
- 尚未进行整机重启、断网故障注入、真实 QQ 发信以及真实模型计费测试。**不能宣称全部长期可靠性验收完成。**

详见 [升级任务 Issue #2](https://github.com/Starlevin/dafeiyu-pi-arm64/issues/2)。生产机私有备份、运行配置和任何密钥均不在仓库中。
