#!/usr/bin/env python3
"""Back up the Swift memory journal and group archives. Restore into a new directory."""

import argparse
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
    r"(?:state\.json(?:\.bak)?|archive\.jsonl|journal/(?:cursor\.json(?:\.bak)?|seg-[0-9]{6}\.log))"
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


def restore(bundle_path, destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new restore directory")
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
        summary = validate_payload(files)
    with tempfile.TemporaryDirectory(prefix=".swift-memory-restore-", dir=destination.parent) as work:
        stage = Path(work) / "payload"
        stage.mkdir(mode=0o700)
        for name, content in files.items():
            target = stage / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    args = parser.parse_args()
    os.umask(0o077)
    operation = create if args.operation == "create" else restore
    print(json.dumps(operation(args.source, args.destination), sort_keys=True))


if __name__ == "__main__":
    main()
