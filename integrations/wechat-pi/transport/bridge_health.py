"""Safe, local WeChat bridge telemetry and outbound deduplication.

No message text or credentials are stored. The outbound ledger is protected by
an OS flock, so two bridge processes cannot reserve the same trace together.
Uncertain sends are never automatically replayed.
"""
import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, get_native_id
import time


def generation_worker():
    """Identify the current Linux worker across process and thread ID reuse."""
    pid, thread = os.getpid(), get_native_id()
    try:
        stat = Path(f"/proc/{pid}/task/{thread}/stat").read_text(encoding="utf-8")
        start_ticks = stat.rsplit(")", 1)[1].split()[19]
    except (OSError, UnicodeError, IndexError):
        return None
    return {"pid": pid, "thread": thread, "start_ticks": start_ticks}


class Health:
    MAX_TRACES = 2048
    TRACE_RETENTION_SECONDS = 30 * 86400
    INGRESS_RETENTION_SECONDS = 0.9
    MAX_DELIVERY_TIMES = 8
    DELIVERY_RETENTION_SECONDS = 10
    VALID_STATUSES = frozenset(("submitting", "unknown", "confirmed"))

    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock = Lock()
        self.state = {"generation_tracking": True}
        self.outbound = {}
        self._reload_unlocked()

    def _reload_unlocked(self):
        """Reload on every mutation: another process may have advanced ledger."""
        try:
            prior = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            prior = {}
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(prior, dict):
            return False
        for field in ("client", "last_event", "last_failure",
                      "last_success", "last_unknown"):
            if isinstance(prior.get(field), dict):
                self.state[field] = prior[field]
        delivery_at = prior.get("last_confirmed_delivery_at")
        self.state.pop("last_confirmed_delivery_at", None)
        if type(delivery_at) in (int, float) and 0 < delivery_at <= 4102444800:
            self.state["last_confirmed_delivery_at"] = delivery_at
        self.state.pop("confirmed_delivery_times", None)
        delivery_times = prior.get("confirmed_delivery_times")
        if isinstance(delivery_times, list):
            valid_times = {at for at in delivery_times[-self.MAX_DELIVERY_TIMES:]
                           if type(at) in (int, float) and 0 < at <= 4102444800}
            if valid_times:
                self.state["confirmed_delivery_times"] = sorted(valid_times)
        self.state.pop("active_generation", None)
        if isinstance(prior.get("active_generation"), dict):
            self.state["active_generation"] = prior["active_generation"]
        self.state.pop("last_ingress", None)
        ingress = prior.get("last_ingress")
        if (isinstance(ingress, dict)
                and isinstance(ingress.get("time"), (int, float))
                and not isinstance(ingress.get("time"), bool)
                and 0 <= ingress["time"] <= 4102444800
                and isinstance(ingress.get("fingerprint"), str)
                and re.fullmatch(r"[0-9a-f]{64}", ingress["fingerprint"])):
            self.state["last_ingress"] = {
                "time": float(ingress["time"]),
                "fingerprint": ingress["fingerprint"],
            }
        rows = prior.get("outbound_traces", {})
        if not isinstance(rows, dict):
            return False
        valid = {}
        for key, value in rows.items():
            if (isinstance(key, str) and len(key) == 64
                    and isinstance(value, dict)
                    and value.get("status") in self.VALID_STATUSES
                    and isinstance(value.get("time"), int)):
                valid[key] = {"status": value["status"], "time": value["time"]}
        self.outbound = valid
        return True

    @contextmanager
    def _disk_lock(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A sidecar inode must not be replaced: flock is advisory and per-inode.
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _prune(self):
        # Preserve the existing integer-second retention semantics for the
        # outbound receipt ledger; it can contain up to MAX_TRACES entries.
        now = int(time.time())
        self.outbound = {
            key: value for key, value in self.outbound.items()
            if 0 <= now - value["time"] <= self.TRACE_RETENTION_SECONDS
        }
        self._prune_ingress()
        if len(self.outbound) > self.MAX_TRACES:
            latest = sorted(self.outbound.items(),
                            key=lambda entry: entry[1]["time"], reverse=True)
            self.outbound = dict(latest[:self.MAX_TRACES])

    def _prune_ingress(self):
        """Expire the single ingress hint in O(1), independent of send history."""
        ingress = self.state.get("last_ingress")
        if isinstance(ingress, dict):
            now = time.time()
            age = now - ingress.get("time", now)
            if age < 0 or age > self.INGRESS_RETENTION_SECONDS:
                self.state.pop("last_ingress", None)

    def _save_unlocked(self):
        """Caller must hold the process lock and the on-disk flock."""
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        payload = dict(self.state)
        payload["outbound_traces"] = self.outbound
        try:
            with tmp.open("w", encoding="utf-8") as file:
                os.chmod(tmp, 0o600)
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            # Make the directory-entry replacement durable on Linux.
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            print("bridge_health_write_failed", flush=True)
            return False

    def is_terminal(self, trace):
        """Fail closed if the ledger cannot be reliably consulted."""
        if not isinstance(trace, str) or len(trace) != 64:
            return False  # old tests without a production trace remain valid
        with self.lock:
            try:
                with self._disk_lock():
                    if not self._reload_unlocked():
                        return True
                    return trace in self.outbound
            except OSError:
                return True

    def reserve_submit(self, trace):
        """Durably reserve before the UI Return key. Never auto-replay unknown."""
        if not isinstance(trace, str) or len(trace) != 64:
            return False
        with self.lock:
            try:
                with self._disk_lock():
                    if not self._reload_unlocked():
                        return False
                    self._prune()
                    if trace in self.outbound:
                        return False
                    self.outbound[trace] = {
                        "status": "submitting", "time": int(time.time())
                    }
                    if self._save_unlocked():
                        return True
                    self.outbound.pop(trace, None)
                    return False
            except OSError:
                return False

    def finish_submit(self, trace, confirmed):
        with self.lock:
            try:
                with self._disk_lock():
                    if not self._reload_unlocked() or trace not in self.outbound:
                        return False
                    self.outbound[trace] = {
                        "status": "confirmed" if confirmed else "unknown",
                        "time": int(time.time()),
                    }
                    return self._save_unlocked()
            except OSError:
                return False

    def finish_generation(self, trace):
        """Clear only the generation owned by this worker."""
        with self.lock:
            try:
                with self._disk_lock():
                    if not self._reload_unlocked():
                        return
                    marker = self.state.get("active_generation")
                    if (marker and marker.get("trace") == trace
                            and marker.get("pid") == os.getpid()
                            and marker.get("thread") == get_native_id()):
                        self.state.pop("active_generation")
                        self._save_unlocked()
            except OSError:
                print("bridge_health_write_failed", flush=True)

    def update(self, phase, reason, trace=None, *,
               failed=False, success=False, unknown=False):
        event = {"time": time.time(), "phase": phase, "reason": reason}
        if trace:
            event["trace"] = trace
        with self.lock:
            try:
                with self._disk_lock():
                    if not self._reload_unlocked():
                        print("bridge_health_read_failed", flush=True)
                        return
                    self._prune_ingress()
                    if trace:
                        self.state["last_event"] = event
                    else:
                        self.state["client"] = event
                    if phase == "generation" and reason == "started" and trace:
                        self.state.pop("active_generation", None)
                        owner = generation_worker()
                        if owner is not None:
                            self.state["active_generation"] = {
                                **owner, "trace": trace, "time": float(event["time"]),
                            }
                    if failed:
                        self.state["last_failure"] = event
                    if success:
                        self.state["last_success"] = event
                        if (not failed and not unknown
                                and (phase, reason) in (("confirmation", "visible_outgoing"),
                                                       ("sticker", "confirmed"))
                                and self.outbound.get(trace, {}).get("status") == "confirmed"):
                            at = event["time"]
                            previous = self.state.get("last_confirmed_delivery_at", 0.0)
                            self.state["last_confirmed_delivery_at"] = max(at, previous)
                            delivery_times = self.state.get("confirmed_delivery_times", [])
                            recent = {stamp for stamp in (*delivery_times, previous, at)
                                      if stamp > 0 and abs(stamp - at) < self.DELIVERY_RETENTION_SECONDS}
                            # Keep verified timestamps only, even when the next
                            # bridge event is a diagnostic for another message.
                            self.state["confirmed_delivery_times"] = sorted(recent)[-self.MAX_DELIVERY_TIMES:]
                    if unknown:
                        self.state["last_unknown"] = event
                    # Only the post-guard queue acceptance is eligible. Keep a
                    # short-lived digest so /live can observe ingress if a fast
                    # generation update replaces last_event before its next tick.
                    # Never persist the source trace, text, account, or user ID here.
                    if (phase == "queue" and reason == "accepted"
                            and isinstance(trace, str)
                            and re.fullmatch(r"[0-9a-fA-F]{64}", trace)):
                        fingerprint = hashlib.sha256(trace.lower().encode("ascii")).hexdigest()
                        previous = self.state.get("last_ingress")
                        if not (isinstance(previous, dict)
                                and previous.get("fingerprint") == fingerprint):
                            self.state["last_ingress"] = {
                                "time": time.time(), "fingerprint": fingerprint
                            }
                    self._save_unlocked()
            except OSError:
                print("bridge_health_write_failed", flush=True)
