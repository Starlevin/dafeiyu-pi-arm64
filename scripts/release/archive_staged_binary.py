"""Archive an inactive ARM64 staging binary after confirming an external copy."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def verify_stream(stream, size, expected_sha, *, packed=None, check_elf=False):
    digest = hashlib.sha256()
    length = 0
    while chunk := stream.read(1024 * 1024):
        if check_elf and length == 0:
            if chunk[:6] != b"\x7fELF\x02\x01" or chunk[18:20] != b"\xb7\0":
                raise ValueError("expected a Linux ARM64 binary")
        length += len(chunk)
        if length > size:
            raise ValueError("artifact is larger than the verified copy")
        digest.update(chunk)
        if packed is not None:
            packed.write(chunk)
    if length != size or digest.hexdigest() != expected_sha:
        raise ValueError("artifact does not match the verified copy")


def refuse_live_artifact(source, manifests):
    for manifest in manifests:
        payload = manifest.read_bytes()
        contents = json.loads(payload)
        if not isinstance(contents, dict) or not isinstance(contents.get("files"), list):
            raise ValueError("unrecognized production manifest")
        for entry in contents["files"]:
            if not isinstance(entry, dict):
                raise ValueError("unrecognized production manifest entry")
            for field in ("path", "rollback_backup"):
                value = entry.get(field)
                if isinstance(value, str) and Path(value).resolve() == source:
                    raise ValueError("artifact is referenced by a production manifest")
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if entry.name.isdigit():
                try:
                    executable = os.readlink(entry / "exe")
                except OSError:
                    continue
                if executable == str(source):
                    raise ValueError("artifact is running")


def sync_directory(directory):
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def archive_binary(source, size, expected_sha, release, running_manifest=None, *, apply=False):
    source = source.absolute()
    release = release.resolve(strict=True)
    staging = release / "staging"
    if (source.name != "dafeiyu" or source.parent.parent != staging
            or source.parent.resolve(strict=True) != source.parent):
        raise ValueError("select one dafeiyu binary in a direct release staging directory")
    if size < 20 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("a verified original size and SHA-256 are required")
    manifests = [release / "production_manifest.json"]
    if running_manifest is not None:
        manifests.append(running_manifest)
    refuse_live_artifact(source, manifests)
    archive = source.with_suffix(".gz")
    if archive.is_symlink() or (archive.exists() and not archive.is_file()):
        raise ValueError("archive path is occupied")
    if archive.exists():
        with gzip.open(archive, "rb") as compressed:
            verify_stream(compressed, size, expected_sha, check_elf=True)
        if not source.exists() and not source.is_symlink():
            return "already_archived", archive.stat().st_size

    original = source.lstat()
    if (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1
            or (os.name == "posix" and (original.st_uid != os.getuid() or original.st_mode & 0o111))):
        raise ValueError("artifact must be an inactive, unlinked staging copy owned by the operator")
    if original.st_size != size:
        raise ValueError("artifact size differs from the verified copy")
    if not apply:
        with source.open("rb") as contents:
            verify_stream(contents, size, expected_sha, check_elf=True)
        return "verified", archive.stat().st_size if archive.exists() else None

    if not archive.exists():
        descriptor, name = tempfile.mkstemp(prefix=".dafeiyu-archive-", dir=source.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as contents:
                with gzip.GzipFile(filename=source.name, fileobj=output, mode="wb",
                                   compresslevel=1, mtime=int(original.st_mtime)) as packed:
                    verify_stream(contents, size, expected_sha, packed=packed, check_elf=True)
                output.flush()
                os.fsync(output.fileno())
            with gzip.open(temporary, "rb") as compressed:
                verify_stream(compressed, size, expected_sha, check_elf=True)
            os.link(temporary, archive)
        finally:
            temporary.unlink()
    else:
        with source.open("rb") as contents:
            verify_stream(contents, size, expected_sha, check_elf=True)
    sync_directory(source.parent)
    refuse_live_artifact(source, manifests)
    final = source.lstat()
    if ((final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
            != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns, original.st_ctime_ns)):
        raise ValueError("source changed while archiving")
    source.unlink()
    sync_directory(source.parent)
    return "archived", archive.stat().st_size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("original_size", type=int)
    parser.add_argument("sha256")
    parser.add_argument("--release", type=Path, default=Path.home() / "dafeiyu-test/release")
    parser.add_argument("--running-manifest", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the archive and remove the verified raw staging copy")
    args = parser.parse_args()
    try:
        status, packed_bytes = archive_binary(args.source, args.original_size, args.sha256,
                                               args.release, args.running_manifest, apply=args.apply)
    except (OSError, ValueError, EOFError) as error:
        parser.exit(1, f"ARCHIVE_REFUSED {error}\n")
    print(json.dumps({"status": status, "source": str(args.source), "sha256": args.sha256,
                      "original_bytes": args.original_size, "compressed_bytes": packed_bytes}))


if __name__ == "__main__":
    main()
