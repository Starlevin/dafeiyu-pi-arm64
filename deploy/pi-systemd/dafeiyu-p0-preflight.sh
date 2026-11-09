#!/usr/bin/env bash
# 部署前只读预检（P0）——只检查，不改动、不重启、不启动任何服务。
# 安装位置：~/.local/bin/dafeiyu-p0-preflight.sh
#
# 用途：维护窗口第一步，确认本轮修复可以安全部署；任何一项不满足就退出非 0，
# 由人工决定是否继续。绝不读取或打印账号、令牌、密钥、Cookie、聊天内容，
# 也绝不读取本机私有的升级前备份包。
set -euo pipefail

readonly EX_CONFIG=78
fail=0
note() { printf 'preflight: %s\n' "$*"; }
bad()  { printf 'preflight: FAIL %s\n' "$*" >&2; fail=1; }

BIN="${DAFEIYU_BIN:-$HOME/dafeiyu-test/dafeiyu-linux/dafeiyu}"
PROFILE="${DAFEIYU_RUNTIME_PROFILE:-$HOME/.config/dafeiyu/runtime-profile.sh}"
if [[ -f "$PROFILE" && ! -L "$PROFILE" && "$(stat -c %a "$PROFILE")" == 600 ]]; then
  note "private runtime profile exists with mode=0600"
else
  bad "private runtime profile missing or permission not 0600"
fi
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# 1) 部署产物完整性（只校验可执行位与随附资源，不触碰状态文件）。
if [[ -x "$BIN" ]]; then
  note "engine binary exists and is executable"
  REAL="$(readlink -f -- "$BIN")"
  for asset in index.html app.js; do
    [[ -s "$(dirname -- "$REAL")/Resources/QQControl/$asset" ]] \
      && note "panel asset present: $asset" \
      || bad "panel asset missing: $asset"
  done
else
  bad "engine binary not executable: $BIN"
fi

# 2) 现有引擎实例：只数个数，不 kill、不读 cmdline。
#    用真实可执行路径匹配，避免把参数里可能出现的敏感文本带进输出。
engine_count=0
if [[ -x "$BIN" ]]; then
  REAL="$(readlink -f -- "$BIN")"
  for proc in /proc/[0-9]*; do
    exe="$(readlink -f -- "$proc/exe" 2>/dev/null)" || continue
    [[ "$exe" == "$REAL" ]] && engine_count=$((engine_count + 1))
  done
fi
note "engine instances (by real exe path): $engine_count"
if (( engine_count > 1 )); then
  bad "multiple engine instances detected; resolve manually before deployment (no kill performed)"
fi

# 3) 单元现状：只读 systemctl show，绝不 start/stop/restart/enable/disable。
if command -v systemctl >/dev/null 2>&1; then
  for unit in dafeiyu.service napcat-qq.service qq-recovery.timer; do
    state="$(systemctl --user show "$unit" --property=ActiveState --value --no-pager 2>/dev/null || printf 'unknown')"
    enabled="$(systemctl --user is-enabled "$unit" 2>/dev/null || printf 'unknown')"
    note "$unit state=$state enabled=$enabled"
  done
  # 期望：dafeiyu.service 仍为 inactive/disabled（现场回退后的安全状态），
  # napcat-qq.service 仍是 enabled/inactive，不得盲目启动。
  dafeiyu_state="$(systemctl --user show dafeiyu.service --property=ActiveState --value --no-pager 2>/dev/null || printf 'unknown')"
  [[ "$dafeiyu_state" != "active" ]] || bad "dafeiyu.service is active; stop-first workflow must be reviewed before deployment"
else
  bad "systemctl not available in PATH"
fi

# 4) 旧的临时超时 drop-in：应存在并计划删除，但不能由本脚本删除。
if [[ -f "$UNIT_DIR/dafeiyu.service.d/10-startup-timeout.conf" ]]; then
  note "legacy 10-startup-timeout.conf present (maintenance window will remove it; preflight does not)"
else
  note "legacy startup timeout drop-in absent"
fi

# 5) 预检脚本自身绝不接触私有备份包、账号配置、日志正文。
note "no credentials, account state, backups or chat data were read"

if (( fail )); then
  printf 'preflight: RESULT=FAIL (no changes made)\n' >&2
  exit "$EX_CONFIG"
fi
printf 'preflight: RESULT=OK (no changes made)\n'
