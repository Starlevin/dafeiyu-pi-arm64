#!/usr/bin/env bash
# run_pi_bridge.sh — 启动 Pi 微信↔core 桥接（设置好 AT-SPI/X11 环境）
# 用法：./run_pi_bridge.sh --group "$DAFEIYU_WECHAT_SPECIAL_GROUP" --bot "$DAFEIYU_WECHAT_BOT_NAME"
set -euo pipefail
export LC_ALL=C
R=${DAFEIYU_WECHAT_HOME:-"$HOME/.local/share/dafeiyu-wechat"}
RT=
for ((attempt=0; attempt<45; attempt++)); do
  RT=$(readlink -f "$R/client-session/runtime" 2>/dev/null) || RT=
  if [[ $RT == "$R/client-session"/.runtime.* && -S "$RT/at-spi/bus_99" ]]; then
    break
  fi
  sleep 1
done
if [[ $RT != "$R/client-session"/.runtime.* || ! -S "$RT/at-spi/bus_99" ]]; then
  echo "微信无障碍会话未就绪" >&2
  exit 2
fi

export GI_TYPELIB_PATH="$R/atspi-typelib/usr/lib/aarch64-linux-gnu/girepository-1.0"
export DISPLAY=:99
export XAUTHORITY="$RT/Xauthority"
export XDG_RUNTIME_DIR="$RT"
export AT_SPI_BUS_ADDRESS="unix:path=$RT/at-spi/bus_99"
# Optional features remain off unless explicitly enabled in the service environment.

exec python3 "$(dirname "$0")/pi_bridge.py" "$@"
