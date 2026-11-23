# 大肥鱼动态家园

`server.py` 提供单机家园、壁纸界面和只读事件推送。控制台按配置代理同源家园。默认只绑定回环地址。

```sh
python3 integrations/fish-house-lan/server.py --host 127.0.0.1 --port 18777
```

在本机打开 `http://127.0.0.1:18777/live` 查看壁纸，或打开 `/fish_house/home.html` 查看家园。部署到可信局域网时，将 `--host` 指向设备的局域网地址，并用 `FISH_HOUSE_LAN_CIDR` 指定允许访问的网段。要读取本机 QQ 状态，可用 `FISH_HOUSE_QQ_SRC` 指向含控制台适配器和本机配置的目录。

## 素材

源代码提供静态房间和主题配置。运行动态播放器时，将[上游默认主题包](https://github.com/jiuchai/fish-house/blob/a3245661ea5bc659fb8656f4ddd21002eed6c26e/assets/default_theme.zip)中的 `videos/` 放到 `web/fish_house/themes/default/`，保留文件名。上游许可和固定主题来源一并见主题目录。成年及 Q 版角色可在家园静态目录的 `home/packs/` 安装符合各自授权的本地素材。
