import argparse
from datetime import datetime
import json
from pathlib import Path
import socket
import subprocess
import time
from zoneinfo import ZoneInfo
from runtime_paths import state_home

def nightly(now):
    return (now.hour <= 5 and now.minute >= 54) or (1 <= now.hour <= 6 and now.minute <= 3)

def reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False

def external_reachable():
    try:
        lookup = subprocess.run(["getent", "ahostsv4", "www.qq.com"], timeout=4,
                                capture_output=True, text=True, check=True)
        addresses = lookup.stdout.split()
        return bool(addresses) and reachable(addresses[0], 443)
    except (OSError, subprocess.SubprocessError):
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nightly", action="store_true")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if args.nightly and not nightly(now):
        return
    root = state_home()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        health = json.loads((root / "bridge-health.json").read_text())
    except (OSError, ValueError):
        health = {}
    def fresh(field):
        event = health.get(field) or {}
        return event if time.time() - event.get("time", 0) <= 60 else {}
    mem = {k.rstrip(":"): int(v) for k, v, *_ in
           (line.split() for line in Path("/proc/meminfo").read_text().splitlines())
           if k in ("MemAvailable:", "SwapFree:")}
    swap = {k: int(v) for k, v in (line.split() for line in Path("/proc/vmstat").read_text().splitlines())
            if k in ("pswpin", "pswpout", "oom_kill")}
    sample = {"time": now.isoformat(timespec="seconds"),
              "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
              "wechat_core": reachable("127.0.0.1", 19301), "qq_onebot": reachable("127.0.0.1", 3001),
              "external_tcp": external_reachable(),
              "client": fresh("client").get("reason", "unknown"),
              "last_success": (health.get("last_success") or {}).get("time"),
              **mem, **swap}
    dest = root / "nightly.jsonl"
    if dest.exists() and dest.stat().st_size >= 512000:
        dest.replace(dest.with_suffix(".previous.jsonl"))
    with dest.open("a") as out:
        out.write(json.dumps(sample) + "\n")
    print(json.dumps(sample))

if __name__ == "__main__":
    main()
