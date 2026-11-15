#!/usr/bin/env bash
# 大肥鱼 P0 单实例前台启动器；只由 dafeiyu.service 使用。
# 绝不 kill 旧实例，不执行长时间状态轮询，不启动第二个 QQ。
set -euo pipefail
umask 077
readonly EX_CONFIG=78
log() { printf 'dafeiyu-launch: %s\n' "$*" >&2; }

# 从旧部署脚本生成的 0600 私有配置读取原有环境变量（仅路径/参数，不复制密钥正文）。
PROFILE="${DAFEIYU_RUNTIME_PROFILE:-$HOME/.config/dafeiyu/runtime-profile.sh}"
[[ -f "$PROFILE" && ! -L "$PROFILE" ]] || { log "运行配置缺失或为软链接，拒绝启动"; exit "$EX_CONFIG"; }
[[ "$(stat -c '%u' "$PROFILE")" == "$(id -u)" ]] || { log "运行配置所有者不正确"; exit "$EX_CONFIG"; }
[[ "$(stat -c '%a' "$PROFILE")" == "600" ]] || { log "运行配置必须为 0600"; exit "$EX_CONFIG"; }
# shellcheck source=/dev/null
source "$PROFILE"

BASE="${DAFEIYU_BASE:-$HOME/dafeiyu-test}"
BIN="${DAFEIYU_BIN:-$BASE/dafeiyu-linux/dafeiyu}"
LOCK_FILE="${DAFEIYU_LAUNCH_LOCK:-$HOME/.local/share/dafeiyu/dafeiyu-launch.lock}"
LOG_FILE="${DAFEIYU_LOG_FILE:-${LOG:-$BASE/run/qq-bot.log}}"
[[ -x "$BIN" ]] || { log "引擎不存在或不可执行"; exit "$EX_CONFIG"; }
REAL_BIN="$(readlink -f -- "$BIN")" || exit "$EX_CONFIG"
BIN_DIR="$(dirname -- "$REAL_BIN")"
for asset in index.html app.js; do
  [[ -s "$BIN_DIR/Resources/QQControl/$asset" ]] || {
    log "面板静态资源缺失：$asset"; exit "$EX_CONFIG"
  }
done
mkdir -p -- "$(dirname -- "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "启动锁占用，拒绝重复启动"
  exit "$EX_CONFIG"
fi
SELF="$$"
for proc in /proc/[0-9]*; do
  pid="${proc##*/}"
  [[ "$pid" == "$SELF" ]] && continue
  exe="$(readlink -f -- "$proc/exe" 2>/dev/null)" || continue
  [[ "$exe" == "$REAL_BIN" ]] || continue
  log "检测到已运行的引擎实例（pid=$pid），拒绝重启或杀进程"
  exit "$EX_CONFIG"
done
# 截断上一代只读面板 URL，防止 enable-target.py 将旧端口和旧令牌用于新进程。
# 日志包含面板控制 URL，只能存放在 0600 的本地文件，绝不写入 systemd journal。
mkdir -p -- "$(dirname -- "$LOG_FILE")"
: > "$LOG_FILE"
chmod 0600 "$LOG_FILE"
export DAFEIYU_RESOURCES="${DAFEIYU_RESOURCES:-$BIN_DIR/Resources}"
cd "$BIN_DIR"
exec "$REAL_BIN" > "$LOG_FILE" 2>&1
