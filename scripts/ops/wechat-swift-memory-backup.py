#!/usr/bin/env python3
"""Back up Swift memory or restore it into a new directory with optional newer withdrawals."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import zipfile

JOURNAL = "unified-pilot-journal.json"
GROUPS = "wechat-group-memory"
MANIFEST = "checkpoint.json"
MAX_BYTES = 16 * 1024 * 1024
MEMORY_FILE = re.compile(
    r"wechat-group-memory/([0-9a-f]{64})/"
    r"(?:state\.json(?:\.bak)?|recall-policy\.json|archive\.jsonl|journal/(?:cursor\.json(?:\.bak)?|seg-[0-9]{6}\.log))"
)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def regular_files(root):
    """List only data files that the Swift memory loader can use."""
    files = [root / JOURNAL]
    groups = root / GROUPS
    if groups.exists():
        if groups.is_symlink() or not groups.is_dir():
            raise ValueError("Invalid group archive directory")
        for scope in groups.iterdir():
            if not re.fullmatch(r"[0-9a-f]{64}", scope.name):
                continue
            if scope.is_symlink() or not scope.is_dir():
                raise ValueError("Invalid group scope directory")
            queue = scope / "journal"
            if queue.exists() and (queue.is_symlink() or not queue.is_dir()):
                raise ValueError("Invalid group journal directory")
            files.extend(p for p in scope.iterdir()
                         if MEMORY_FILE.fullmatch(p.relative_to(root).as_posix()))
            if queue.is_dir():
                files.extend(p for p in queue.iterdir()
                             if MEMORY_FILE.fullmatch(p.relative_to(root).as_posix()))
    rows = {}
    total = 0
    for p in files:
        props = p.lstat()
        if not stat.S_ISREG(props.st_mode):
            raise ValueError("Memory checkpoint requires regular files")
        total += props.st_size
        if total > MAX_BYTES:
            raise ValueError("Memory checkpoint exceeds its size limit")
        rows[p.relative_to(root).as_posix()] = (props.st_ino, props.st_size, props.st_mtime_ns)
    return rows


def validate_payload(files):
    journal = json.loads(files[JOURNAL])
    if (not isinstance(journal, dict) or journal.get("version") != 1
            or not isinstance(journal.get("entries"), list)):
        raise ValueError("Unrecognized Swift journal")
    for name, content in files.items():
        if name == JOURNAL:
            continue
        match = MEMORY_FILE.fullmatch(name)
        if not match:
            raise ValueError("Unexpected file in Swift memory checkpoint")
        if name.endswith(("/state.json", "/state.json.bak")):
            scope = json.loads(content)
            if not isinstance(scope, dict) or scope.get("v") != 1 or scope.get("scope") != match[1]:
                raise ValueError("Group memory scope mismatch")
        elif name.endswith("/recall-policy.json"):
            policy = json.loads(content)
            if not isinstance(policy, dict) or policy.get("version") != 1 or policy.get("scope") != match[1]:
                raise ValueError("Group recall policy scope mismatch")
            topics = policy.get("pausedTopics")
            if (len(content) > 65_536 or not isinstance(topics, list)
                    or any(not isinstance(topic, str) or not topic for topic in topics)):
                raise ValueError("Invalid group recall policy")
    return {"files": len(files), "journalEntries": len(journal["entries"]),
            "scopes": len({name.split("/")[1] for name in files if name.startswith(GROUPS + "/")})}


def capture(root):
    for _ in range(3):
        try:
            before = regular_files(root)
            files = {}
            for name, (_, size, _) in before.items():
                with (root / name).open("rb") as stream:
                    files[name] = stream.read(size + 1)
            if (all(len(files[name]) == props[1] for name, props in before.items())
                    and before == regular_files(root)):
                validate_payload(files)
                return files
        except FileNotFoundError:
            pass
        time.sleep(.15)
    raise ValueError("Could not capture a stable Swift memory checkpoint")


def create(root, output):
    files = capture(root)
    manifest = {name: digest(content) for name, content in files.items()}
    with tempfile.TemporaryDirectory(prefix=".swift-memory-checkpoint-", dir=output.parent) as work:
        staged = Path(work) / "checkpoint.zip"
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr(MANIFEST, json.dumps({"version": 1, "sha256": manifest}, sort_keys=True))
                for name, content in sorted(files.items()):
                    bundle.writestr(name, content)
            handle.flush()
            os.fsync(handle.fileno())
        # Publish the completed archive without replacing an existing checkpoint.
        os.link(staged, output)
    return validate_payload(files)


def read_checkpoint(bundle_path):
    with zipfile.ZipFile(bundle_path) as bundle:
        names = bundle.namelist()
        if len(set(names)) != len(names) or MANIFEST not in names or JOURNAL not in names:
            raise ValueError("Invalid Swift memory checkpoint members")
        if any(name not in (MANIFEST, JOURNAL) and not MEMORY_FILE.fullmatch(name) for name in names):
            raise ValueError("Unexpected file in Swift memory checkpoint")
        if sum(row.file_size for row in bundle.infolist()) > MAX_BYTES + 256 * 1024:
            raise ValueError("Memory checkpoint exceeds its size limit")
        manifest = json.loads(bundle.read(MANIFEST))
        files = {name: bundle.read(name) for name in names if name != MANIFEST}
        checksums = {name: digest(data) for name, data in files.items()}
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("sha256") != checksums:
            raise ValueError("Swift memory checkpoint checksum mismatch")
        validate_payload(files)
        return files


def withdrawal_ids(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("Invalid withdrawal markers in Swift memory")
    return value


def read_withdrawals(files, scope):
    prefix = f"{GROUPS}/{scope}/"
    ids, source_ids = set(), set()
    for suffix in ("state.json", "state.json.bak"):
        if prefix + suffix not in files:
            continue
        state = json.loads(files[prefix + suffix])
        ids.update(withdrawal_ids(state.get("forgottenIDs", [])))
        source_ids.update(withdrawal_ids(state.get("forgottenSourceIDs", [])))
    for line in files.get(prefix + "archive.jsonl", b"").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue  # Swift also ignores incomplete archive lines.
        if not isinstance(record, dict):
            continue
        if record.get("kind") == "tombstone":
            ids.update(withdrawal_ids(record.get("ids", [])))
            source_ids.update(withdrawal_ids(record.get("forgottenSourceIDs", [])))
        elif record.get("kind") == "itemVersion":
            item = record.get("item")
            if isinstance(item, dict) and item.get("reason") == "forget":
                ids.update(withdrawal_ids([item.get("id")]))
                if item.get("kind") != "episode":
                    source_ids.update(withdrawal_ids(item.get("sourceIDs", [])))
    return ids, source_ids


def suppress_withdrawn_turns(files, source_ids_by_scope):
    journal = json.loads(files[JOURNAL])
    changed = 0
    for entry in journal["entries"]:
        if not isinstance(entry, dict):
            continue
        memory, reply = entry.get("memory"), entry.get("reply")
        if not isinstance(memory, dict) or not isinstance(reply, dict):
            continue
        scope, source_id = memory.get("sessionDigest"), reply.get("memoryReceipt")
        if (not isinstance(scope, str) or not isinstance(source_id, str)
                or source_id not in source_ids_by_scope.get(scope, ())):
            continue
        # Keep the idempotency entry, but neither replay the old reply nor use
        # the withdrawn turn as dialogue history after restore.
        entry.pop("memory")
        reply.pop("reply", None)
        reply.pop("memoryReceipt")
        reply.update(ok=True, action="ignore", reason="memory_withdrawn")
        changed += 1
    if changed:
        files[JOURNAL] = json.dumps(journal, ensure_ascii=False, separators=(",", ":")).encode()
    return changed


def preserve_withdrawals(files, newer):
    scopes = {name.split("/")[1] for name in files if name.startswith(GROUPS + "/")}
    changed, source_ids_by_scope = 0, {}
    for scope in sorted(scopes):
        ids, sources = read_withdrawals(newer, scope)
        old_ids, old_sources = read_withdrawals(files, scope)
        if sources or old_sources:
            source_ids_by_scope[scope] = sources | old_sources
        ids.difference_update(old_ids)
        sources.difference_update(old_sources)
        if not ids and not sources:
            continue
        name = f"{GROUPS}/{scope}/archive.jsonl"
        record = {"kind": "tombstone", "ids": sorted(ids), "forgottenSourceIDs": sorted(sources),
                  "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        prior = files.get(name, b"")
        separator = b"\n" if prior and not prior.endswith(b"\n") else b""
        files[name] = prior + separator + json.dumps(record, sort_keys=True).encode() + b"\n"
        changed += 1
    suppressed = suppress_withdrawn_turns(files, source_ids_by_scope) if source_ids_by_scope else 0
    if sum(map(len, files.values())) > MAX_BYTES:
        raise ValueError("Restored Swift memory exceeds its size limit")
    return changed, suppressed


def restore(bundle_path, destination, *, withdrawals_from=None):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new restore directory")
    files = read_checkpoint(bundle_path)
    newer = {}
    if withdrawals_from is not None:
        newer = capture(withdrawals_from) if withdrawals_from.is_dir() else read_checkpoint(withdrawals_from)
    added, suppressed = preserve_withdrawals(files, newer)
    summary = validate_payload(files)
    if withdrawals_from is not None:
        summary["withdrawalScopesMerged"] = added
    if suppressed or withdrawals_from is not None:
        summary["withdrawnTurnsSuppressed"] = suppressed
    with tempfile.TemporaryDirectory(prefix=".swift-memory-restore-", dir=destination.parent) as work:
        stage = Path(work) / "payload"
        stage.mkdir(mode=0o700)
        for name, content in files.items():
            parent = stage
            parts = name.split("/")
            for component in parts[:-1]:
                parent = parent / component
                parent.mkdir(mode=0o700, exist_ok=True)
            target = parent / parts[-1]
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Restore directory became occupied")
        stage.rename(destination)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--preserve-withdrawals-from", type=Path, metavar="DIRECTORY_OR_ZIP",
                        help="Carry same-group withdrawal markers from the current memory directory or a newer checkpoint")
    args = parser.parse_args()
    os.umask(0o077)
    if args.operation == "create":
        if args.preserve_withdrawals_from is not None:
            parser.error("--preserve-withdrawals-from is only for restore")
        result = create(args.source, args.destination)
    else:
        result = restore(args.source, args.destination, withdrawals_from=args.preserve_withdrawals_from)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
