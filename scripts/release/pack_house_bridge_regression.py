"""Pack the Raspberry Pi house and WeChat bridge Python regressions only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


HOUSE = "integrations/fish-house-lan"
BRIDGE = "integrations/wechat-pi/transport"
FILES = (
    f"{HOUSE}/server.py",
    f"{HOUSE}/backend/flow_bridge.py",
    f"{HOUSE}/backend/live_ws.py",
    f"{HOUSE}/backend/status_adapter.py",
    f"{HOUSE}/tests/test_live_ws_bridge.py",
    f"{HOUSE}/tests/test_status_adapter.py",
    f"{HOUSE}/web/fish_house/themes/default/theme.json",
    f"{BRIDGE}/bridge_health.py",
    f"{BRIDGE}/test_bridge_health_ingress.py",
)


def pack(repo: Path, commit: str, output: Path, *, max_staging_bytes: int | None = None) -> dict:
    """Publish a complete archive without replacing an existing file."""
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if max_staging_bytes is not None and max_staging_bytes < 1:
        raise ValueError("staging capacity must be at least one byte")
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", commit + "^{commit}"],
        cwd=repo, timeout=15, text=True,
    ).strip()

    with tempfile.TemporaryDirectory(prefix=".house-bridge-", dir=output.parent) as directory:
        candidate = Path(directory) / "candidate.tar.gz"
        subprocess.run(
            ["git", "archive", "--format=tar.gz", f"--output={candidate}",
             revision, "--", *FILES],
            cwd=repo, check=True, timeout=30,
        )
        with tarfile.open(candidate, "r:gz") as archive:
            files = [entry for entry in archive if not entry.isdir()]
        if ({entry.name for entry in files} != set(FILES) or len(files) != len(FILES)
                or not all(entry.isfile() for entry in files)):
            raise ValueError("archive must contain exactly the regression sources and theme JSON")
        extracted_bytes = sum(entry.size for entry in files)
        archive_bytes = candidate.stat().st_size
        staging_bytes = extracted_bytes + archive_bytes
        if max_staging_bytes is not None and staging_bytes > max_staging_bytes:
            raise ValueError(f"requires {staging_bytes} bytes, budget is {max_staging_bytes} bytes")
        with candidate.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        os.link(candidate, output)

    return {
        "commit": revision, "archive_bytes": archive_bytes, "extracted_bytes": extracted_bytes,
        "staging_bytes": staging_bytes, "sha256": sha256,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--max-staging-bytes", type=int)
    args = parser.parse_args()
    try:
        result = pack(args.repo, args.commit, args.output,
                      max_staging_bytes=args.max_staging_bytes)
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
