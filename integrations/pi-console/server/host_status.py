"""Lightweight Pi metrics sampled by the console's maintenance thread."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

SAMPLE_SECONDS = 60
HISTORY_SAMPLES = 1440
HISTORY_SECONDS = SAMPLE_SECONDS * HISTORY_SAMPLES
COMPACT_AFTER = HISTORY_SAMPLES + 60
MAX_HISTORY_BYTES = COMPACT_AFTER * 1024

SERVICE_UNITS = {
    "qq": "napcat-qq.service",
    "wechatClient": "dafeiyu-wechat-client.service",
    "wechatBridge": "dafeiyu-wechat-bridge.service",
    "core": "dafeiyu.service",
    "modelHub": "model-hub.service",
    "house": "fish-house-lan.service",
}
SERVICE_STATES = {
    "active", "inactive", "activating", "deactivating", "failed",
    "reloading", "maintenance", "refreshing",
}


def _read(path):
    try:
        return path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return ""


def _number(value, minimum=-float("inf"), maximum=float("inf")):
    if (type(value) in (int, float) and -float("inf") < value < float("inf")
            and minimum <= value <= maximum):
        return value
    return None


def _first_number(text):
    try:
        return _number(float(text.split()[0]))
    except (IndexError, ValueError):
        return None


def _capacity(total, available):
    if type(total) is int and type(available) is int and 0 <= available <= total:
        return {"usedBytes": total - available, "totalBytes": total}
    return None


def _service_states():
    states = dict.fromkeys(SERVICE_UNITS, "unknown")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "--no-pager", "show", "--property=Id,LoadState,ActiveState",
             "--", *SERVICE_UNITS.values()],
            capture_output=True, encoding="utf-8", errors="replace", timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return states

    keys_by_unit = {unit: key for key, unit in SERVICE_UNITS.items()}
    # systemctl may return nonzero for a single missing unit while still
    # providing valid blocks for the other requested units.
    for block in result.stdout.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        key = keys_by_unit.get(props.get("Id"))
        state = props.get("ActiveState")
        if key and props.get("LoadState") == "loaded" and state in SERVICE_STATES:
            states[key] = state
    return states


class LinuxHostProbe:
    def __init__(self, disk_path, proc=Path("/proc"),
                 temperature=Path("/sys/class/thermal/thermal_zone0/temp")):
        self.disk_path = disk_path
        self.proc = proc
        self.temperature = temperature
        self.cpu_baseline = None

    def _cpu_percent(self):
        fields = _read(self.proc / "stat").split("\n", 1)[0].split()
        if len(fields) < 5 or fields[0] != "cpu":
            return None
        try:
            # Linux counts guest/guest_nice in user/nice already.
            ticks = tuple(int(field) for field in fields[1:9])
        except ValueError:
            return None
        if min(ticks) < 0:
            return None
        previous, self.cpu_baseline = self.cpu_baseline, ticks
        if previous is None or len(previous) != len(ticks):
            return None
        delta = tuple(current - prior for current, prior in zip(ticks, previous))
        total = sum(delta)
        if total <= 0 or min(delta) < 0:
            return None
        idle = sum(delta[3:5])  # idle and I/O wait
        return round(100 * (total - idle) / total, 1)

    def _memory_and_swap(self):
        values = {}
        for line in _read(self.proc / "meminfo").splitlines():
            key, _, value = line.partition(":")
            if key not in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                continue
            fields = value.split()
            if len(fields) == 2 and fields[1] == "kB":
                try:
                    values[key] = int(fields[0]) * 1024
                except ValueError:
                    continue
        return (
            _capacity(values.get("MemTotal"), values.get("MemAvailable")),
            _capacity(values.get("SwapTotal"), values.get("SwapFree")),
        )

    def measure(self, at):
        memory, swap = self._memory_and_swap()
        try:
            usage = shutil.disk_usage(self.disk_path)
            disk = {"usedBytes": usage.used, "totalBytes": usage.total}
        except OSError:
            disk = None
        load = _first_number(_read(self.proc / "loadavg"))
        uptime = _first_number(_read(self.proc / "uptime"))
        temp = _first_number(_read(self.temperature))
        return {
            "at": at,
            "cpuPercent": self._cpu_percent(),
            "load1": _number(load, 0),
            "tempC": round(temp / 1000, 1) if temp is not None else None,
            "uptimeSeconds": int(uptime) if uptime is not None and uptime >= 0 else None,
            "memory": memory,
            "swap": swap,
            "disk": disk,
            "services": _service_states(),
        }


def _saved_capacity(value):
    if not isinstance(value, dict):
        return None
    total, used = value.get("totalBytes"), value.get("usedBytes")
    if type(total) is int and type(used) is int:
        return _capacity(total, total - used)
    return None


def _saved_sample(item):
    if not isinstance(item, dict) or type(item.get("at")) is not int:
        return None
    services = item.get("services")
    services = services if isinstance(services, dict) else {}
    return {
        "at": item["at"],
        "cpuPercent": _number(item.get("cpuPercent"), 0, 100),
        "load1": _number(item.get("load1"), 0),
        "tempC": _number(item.get("tempC")),
        "uptimeSeconds": _number(item.get("uptimeSeconds"), 0),
        "memory": _saved_capacity(item.get("memory")),
        "swap": _saved_capacity(item.get("swap")),
        "disk": _saved_capacity(item.get("disk")),
        "services": {
            key: services[key] if isinstance(services.get(key), str)
            and services[key] in SERVICE_STATES else "unknown"
            for key in SERVICE_UNITS
        },
    }


class HostStatus:
    """One maintenance writer with a bounded cache shared by HTTP readers."""

    def __init__(self, state_dir, *, enabled, probe=None, clock=time.time):
        self.enabled = enabled
        self._path = state_dir / "host-status.jsonl"
        self._probe = probe if probe is not None else LinuxHostProbe(state_dir)
        self._clock = clock
        self._lock = threading.Lock()
        self._history = deque(maxlen=HISTORY_SAMPLES)
        self._disk_records = 0
        self._needs_compaction = False
        self._write_failed = False
        if enabled:
            self._load()

    def section(self):
        now = int(self._clock())
        with self._lock:
            history = self._recent(now)
        return {"ok": True, "sampleSeconds": SAMPLE_SECONDS,
                "current": history[-1] if history else None, "history": history}

    def _recent(self, now):
        return [sample for sample in self._history
                if now - HISTORY_SECONDS < sample["at"] <= now]

    def _load(self):
        now = int(self._clock())
        try:
            with self._path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                offset = max(0, size - MAX_HISTORY_BYTES)
                stream.seek(offset)
                data = stream.read(MAX_HISTORY_BYTES)
        except FileNotFoundError:
            return
        except OSError:
            self._needs_compaction = True
            return

        lines = data.split(b"\n")
        self._needs_compaction = bool(offset or lines[-1])
        complete = lines[1 if offset else 0:-1]
        self._disk_records = len(complete)
        samples = {}
        for line in complete[-COMPACT_AFTER:]:
            try:
                sample = _saved_sample(json.loads(line))
            except (ValueError, RecursionError):
                continue
            if sample is not None and now - HISTORY_SECONDS < sample["at"] <= now:
                samples[sample["at"]] = sample
        self._history.extend(samples[at] for at in sorted(samples))

    def record(self):
        if not self.enabled:
            return
        at = int(self._clock())
        sample = self._probe.measure(at)
        with self._lock:
            if self._history and self._history[-1]["at"] == at:
                return
            # A wall clock correction must not leave future samples in the cache.
            while self._history and self._history[-1]["at"] >= at:
                self._history.pop()
                self._needs_compaction = True
            self._history.append(sample)
            compact = self._needs_compaction or self._disk_records >= COMPACT_AFTER
            history = self._recent(at) if compact else None

        try:
            if history is not None:
                self._replace(history)
                self._disk_records = len(history)
                self._needs_compaction = False
            else:
                with self._path.open("ab") as stream:
                    stream.write(self._encode(sample))
                self._disk_records += 1
            self._write_failed = False
        except OSError:
            self._needs_compaction = True
            if not self._write_failed:
                logging.getLogger(__name__).warning("host_status_history_write_failed")
            self._write_failed = True

    @staticmethod
    def _encode(sample):
        return json.dumps(sample, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"

    def _replace(self, history):
        with tempfile.NamedTemporaryFile(prefix=".host-status-", dir=self._path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            try:
                for sample in history:
                    stream.write(self._encode(sample))
                stream.close()
                temporary.replace(self._path)
            finally:
                temporary.unlink(missing_ok=True)
