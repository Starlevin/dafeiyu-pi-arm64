#!/usr/bin/env python3
"""从既有部署启动器提取参数引用，生成私有运行环境文件；不读取密钥正文。"""
from __future__ import annotations
import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path

PREAMBLE = ("BASE", "APP", "KEY", "ONEBOT_TOKEN_FILE", "LOG", "TARGET")
RUNTIME = (
    "DAFEIYU_DATA_DIR", "DAFEIYU_RESOURCES", "DAFEIYU_LLM_PROVIDER",
    "DAFEIYU_LLM_BASE_URL", "DAFEIYU_LLM_MODEL", "DAFEIYU_LLM_KEY_FILE",
    "DAFEIYU_SENSENOVA_IMAGE_KEY_FILE", "DAFEIYU_LLM_TIMEOUT",
    "DAFEIYU_ONEBOT_TOKEN_FILE", "DAFEIYU_ALLOWED_TARGETS", "DAFEIYU_AUTO_RESUME",
    "DAFEIYU_RECONNECT_MAX_ATTEMPTS", "DAFEIYU_RECONNECT_MAX_DELAY",
    "DAFEIYU_REPLY_MODE", "DAFEIYU_REVIEW_EFFORT", "DAFEIYU_IMAGE_INPUT",
    "DAFEIYU_PERF_LOG",
)
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def build_profile(source: str) -> str:
    assignments = {}
    for line in source.splitlines():
        s = line.strip()
        if s.endswith("\\"):
            s = s[:-1].rstrip()
        # 原脚本 TARGET 后的非执行注释；只复制赋值表达式。
        s = re.split(r"\s+#\s", s, maxsplit=1)[0].strip()
        m = ASSIGNMENT.fullmatch(s)
        if m and m.group(1) in (*PREAMBLE, *RUNTIME):
            name = m.group(1)
            if name in assignments:
                raise ValueError("重复定义运行环境变量: " + name)
            assignments[name] = s
    missing = [n for n in (*PREAMBLE, *RUNTIME) if n not in assignments]
    if missing:
        raise ValueError("旧启动器缺少预期配置项：" + ",".join(missing))
    out = ["#!/usr/bin/env bash",
           "# 来自用户现有启动脚本的受控配置快照（只存引用和配置，不含密钥正文）",
           "# 权限必须为 0600，禁止提交到 GitHub 或上传 Notion。"]
    out.extend(assignments[name] for name in PREAMBLE)
    out.extend("export " + assignments[name] for name in RUNTIME)
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path,
                        default=Path.home() / "dafeiyu-test/start-dafeiyu-autostart.sh")
    parser.add_argument("--output", type=Path,
                        default=Path.home() / ".config/dafeiyu/runtime-profile.sh")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    profile = build_profile(args.source.read_text(encoding="utf-8"))
    check = subprocess.run(["bash", "-n"], input=profile, text=True,
                           capture_output=True, check=False)
    if check.returncode:
        raise SystemExit("生成结果 shell 语法检查失败（未写文件）")
    if args.check_only:
        print("PROFILE_CHECK_OK; no credential contents accessed; no changes")
        return 0
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                     dir=str(args.output.parent), delete=False) as f:
        tmp = Path(f.name)
        os.chmod(tmp, 0o600)
        f.write(profile)
    try:
        if args.output.exists():
            raise FileExistsError("配置已存在；拒绝覆盖，请人工检查")
        os.replace(tmp, args.output)
    finally:
        if tmp.exists():
            tmp.unlink()
    print("PROFILE_CREATED mode=0600 (path omitted; do not share file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
