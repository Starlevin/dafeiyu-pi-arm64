"""Offline tests for Pi probes, bounded history, and degradation."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import host_status as hs


class HostProbeTests(unittest.TestCase):
    def test_probe_reports_cpu_deltas_and_isolates_unavailable_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            temp = proc / "temp"
            stat = proc / "stat"
            stat.write_text("cpu 10 0 10 20 5 0 0 0 10 0\ncpu0 10 0 10 20 5 0 0 0\n")
            temp.write_text("47320\n")
            (proc / "loadavg").write_text("0.33 0.25 0.20 1/177 2023\n")
            (proc / "uptime").write_text("123.4 456.7\n")
            (proc / "meminfo").write_text(
                "MemTotal: 1000 kB\nMemAvailable: 250 kB\n"
                "SwapTotal: 0 kB\nSwapFree: 0 kB\n"
            )
            probe = hs.LinuxHostProbe(proc, proc, temp)
            units = dict.fromkeys(hs.SERVICE_UNITS, "active")
            with patch.object(hs, "_service_states", return_value=units), patch.object(
                hs.shutil, "disk_usage", return_value=SimpleNamespace(total=1000, used=900, free=80)
            ) as disk:
                first = probe.measure(100)
                self.assertEqual(first, {
                    "at": 100, "cpuPercent": None, "load1": 0.33, "tempC": 47.3,
                    "uptimeSeconds": 123,
                    "memory": {"usedBytes": 750 * 1024, "totalBytes": 1000 * 1024},
                    "swap": {"usedBytes": 0, "totalBytes": 0},
                    "disk": {"usedBytes": 900, "totalBytes": 1000},
                    "services": units,
                })
                stat.write_text("cpu 30 0 20 35 10 0 0 0 30 0\n")
                self.assertEqual(probe.measure(160)["cpuPercent"], 60.0)
                stat.write_text("cpu 1 0 1 2 1 0 0 0\n")
                temp.unlink()
                (proc / "meminfo").write_text(
                    "MemTotal: broken kB\nMemAvailable: 250 kB\n"
                    "SwapTotal: 20 kB\nSwapFree: 10 kB\n"
                )
                (proc / "loadavg").write_text("NaN")
                disk.side_effect = OSError
                partial = probe.measure(220)
                for field in ("cpuPercent", "tempC", "load1", "memory", "disk"):
                    self.assertIsNone(partial[field], field)
                self.assertEqual(partial["uptimeSeconds"], 123)
                self.assertEqual(partial["swap"], {"usedBytes": 10240, "totalBytes": 20480})

    def test_systemd_is_one_short_read_with_exact_units_and_known_states(self):
        stdout = (
            "Id=napcat-qq.service\nLoadState=loaded\nActiveState=active\n\n"
            "Id=model-hub.service\nLoadState=loaded\nActiveState=failed\n\n"
            "Id=fish-house-lan.service\nLoadState=not-found\nActiveState=inactive\n\n"
            "Id=dafeiyu.service\nLoadState=loaded\nActiveState=unexpected\n\n"
            "Id=unrelated.service\nLoadState=loaded\nActiveState=active\n"
        )
        with patch.object(hs.subprocess, "run", return_value=SimpleNamespace(stdout=stdout)) as run:
            states = hs._service_states()
            run.assert_called_once()
            args = run.call_args.args[0]
            self.assertEqual(args[:4], ["systemctl", "--user", "--no-pager", "show"])
            self.assertEqual(args[6:], list(hs.SERVICE_UNITS.values()))
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            self.assertEqual(states, dict.fromkeys(hs.SERVICE_UNITS, "unknown")
                             | {"qq": "active", "modelHub": "failed"})
            run.side_effect = subprocess.TimeoutExpired(args, 2)
            self.assertEqual(hs._service_states(), dict.fromkeys(hs.SERVICE_UNITS, "unknown"))


class HostHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.file = self.path / "host-status.jsonl"
        self.now = 1_800_000_000
        self.probe = SimpleNamespace(measure=lambda at: hs._saved_sample({"at": at}))

    def recorder(self, enabled=True):
        return hs.HostStatus(
            self.path, enabled=enabled, probe=self.probe, clock=lambda: self.now,
        )

    def test_disabled_recorder_has_no_background_metrics_or_disk_writes(self):
        recorder = self.recorder(enabled=False)
        with patch.object(self.probe, "measure", side_effect=AssertionError("must not probe")):
            recorder.record()
        self.assertFalse(self.file.exists())
        self.assertEqual(recorder.section(), {
            "ok": True, "sampleSeconds": 60, "current": None, "history": [],
        })

    def test_cold_start_recovers_valid_history_and_repairs_a_torn_record(self):
        saved = [
            {"at": self.now - 60, "cpuPercent": 51},
            {"at": self.now - hs.HISTORY_SECONDS, "cpuPercent": 70},
            {"at": self.now - 120, "cpuPercent": 35},
            {"at": self.now - 60, "cpuPercent": 42, "unexpectedField": "discard",
             "services": {"house": "active", "core": [], "other": "active"}},
            {"at": self.now + 60, "cpuPercent": 99},
        ]
        self.file.write_bytes(b"".join(
            json.dumps(sample).encode("ascii") + b"\n" for sample in saved
        ) + b"bad line\n" + b'{"at":')

        recorder = self.recorder()
        section = recorder.section()
        self.assertEqual([sample["at"] for sample in section["history"]],
                         [self.now - 120, self.now - 60])
        self.assertEqual(section["current"]["cpuPercent"], 42)
        self.assertNotIn("unexpectedField", section["current"])
        self.assertEqual(section["current"]["services"],
                         dict.fromkeys(hs.SERVICE_UNITS, "unknown") | {"house": "active"})
        recorder.record()
        self.assertEqual(recorder.section(), self.recorder().section())
        self.assertEqual(len(self.file.read_bytes().splitlines()), 3)

    def test_appends_between_compactions_and_keeps_only_the_latest_24_hours(self):
        recorder = self.recorder()
        with patch.object(recorder, "_replace", wraps=recorder._replace) as compact:
            for _ in range(hs.COMPACT_AFTER + 2):
                self.now += hs.SAMPLE_SECONDS
                recorder.record()
            compact.assert_called_once()
        history = recorder.section()["history"]
        self.assertEqual(len(history), hs.HISTORY_SAMPLES)
        self.assertEqual(history[0]["at"], self.now - hs.HISTORY_SECONDS + hs.SAMPLE_SECONDS)
        self.assertEqual(history[-1]["at"], self.now)
        self.assertEqual(len(self.file.read_bytes().splitlines()), hs.HISTORY_SAMPLES + 1)
        self.assertEqual(self.recorder().section(), recorder.section())

        # A backwards clock step replaces the old future and duplicate ticks.
        self.now -= 120
        recorder.record()
        history = recorder.section()["history"]
        self.assertEqual(history[-1]["at"], self.now)
        self.assertEqual([sample["at"] for sample in history],
                         sorted({sample["at"] for sample in history}))
        self.assertEqual(self.recorder().section(), recorder.section())

    def test_disk_failure_preserves_in_memory_readout_and_recovers_on_next_tick(self):
        recorder = self.recorder()
        with patch.object(Path, "open", side_effect=OSError("fixture")), self.assertLogs(
            "host_status", "WARNING"
        ):
            recorder.record()
        self.assertFalse(self.file.exists())
        self.assertEqual(recorder.section()["current"]["at"], self.now)
        self.now += 60
        recorder.record()
        self.assertEqual(len(self.recorder().section()["history"]), 2)


if __name__ == "__main__":
    unittest.main()
