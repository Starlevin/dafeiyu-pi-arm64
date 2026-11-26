"""Offline regression tests for scripts/release/pack_house_bridge_regression.pack()."""
import hashlib
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from pack_house_bridge_regression import FILES, pack


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise AssertionError(
            f"git {args} failed ({result.returncode}): {result.stderr}")
    return result.stdout.strip()


class PackHouseBridgeRegressionTests(unittest.TestCase):
    """Every fixture is a throwaway local Git repo; no real chat/credential data."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="pack-house-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Fixture")
        # Keep blob bytes identical to what we write, so byte counts are exact.
        git(self.repo, "config", "core.autocrlf", "false")
        self.output = self.root / "house-bridge.tar.gz"

    def write_sources(self, version):
        expected = {}
        for rel in FILES:
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            content = f"fixture {version} :: {rel}\n".encode("utf-8")
            target.write_bytes(content)
            expected[rel] = content
        return expected

    def write_extras(self):
        """Files that must never be packed even though they are committed."""
        for rel in (
            "integrations/fish-house-lan/web/fish_house/themes/default/clip.mp4",
            "integrations/fish-house-lan/evidence/session.json",
            "integrations/fish-house-lan/backend/notes.tmp",
        ):
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"EXTRA-CONTENT-MUST-STAY-OUT\n")

    def commit_all(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def tar_members(self, output):
        with tarfile.open(output, "r:gz") as archive:
            return {entry.name: archive.extractfile(entry).read()
                    for entry in archive if not entry.isdir()}

    def test_contract_counts_and_sha256(self):
        expected = self.write_sources("v1")
        self.write_extras()
        commit = self.commit_all("fixture v1")

        result = pack(self.repo, commit, self.output)

        self.assertEqual(set(result), {
            "commit", "archive_bytes", "extracted_bytes", "staging_bytes", "sha256"})
        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["archive_bytes"], self.output.stat().st_size)
        self.assertEqual(result["extracted_bytes"],
                         sum(len(content) for content in expected.values()))
        self.assertEqual(result["staging_bytes"],
                         result["archive_bytes"] + result["extracted_bytes"])
        self.assertEqual(result["sha256"],
                         hashlib.sha256(self.output.read_bytes()).hexdigest())

        members = self.tar_members(self.output)
        self.assertEqual(set(members), set(FILES))
        self.assertEqual(len(members), len(FILES))
        self.assertEqual(members, expected)

    def test_uses_requested_commit_not_head_or_uncommitted_content(self):
        v1 = self.write_sources("v1")
        self.write_extras()
        commit1 = self.commit_all("fixture v1")

        v2 = self.write_sources("v2")
        commit2 = self.commit_all("fixture v2")
        self.assertEqual(commit1, git(self.repo, "rev-parse", commit1))

        # Dirty the working tree after HEAD: archive must still use committed bytes.
        dirty = self.repo / FILES[0]
        dirty.write_bytes(b"UNCOMMITTED-WORKTREE-CONTENT\n")

        first = self.root / "first.tar.gz"
        result1 = pack(self.repo, commit1, first)
        self.assertEqual(result1["commit"], commit1)
        self.assertEqual(self.tar_members(first), v1)

        second = self.root / "second.tar.gz"
        result2 = pack(self.repo, "HEAD", second)
        self.assertEqual(result2["commit"], commit2)
        self.assertEqual(self.tar_members(second), v2)

    def test_max_staging_bytes_over_budget_leaves_no_output(self):
        self.write_sources("v1")
        commit = self.commit_all("fixture v1")
        probe = self.root / "probe.tar.gz"
        baseline = pack(self.repo, commit, probe)

        with self.assertRaises(ValueError):
            pack(self.repo, commit, self.output,
                 max_staging_bytes=baseline["staging_bytes"] - 1)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.is_symlink())
        # Nothing was published to the probe output beyond its own archive.
        self.assertTrue(probe.exists())

    def test_max_staging_bytes_boundary_is_inclusive(self):
        self.write_sources("v1")
        commit = self.commit_all("fixture v1")
        probe = self.root / "probe.tar.gz"
        baseline = pack(self.repo, commit, probe)

        result = pack(self.repo, commit, self.output,
                      max_staging_bytes=baseline["staging_bytes"])
        self.assertEqual(result["staging_bytes"], baseline["staging_bytes"])
        self.assertTrue(self.output.exists())
        with self.assertRaises(ValueError):
            pack(self.repo, commit, self.root / "bad-budget.tar.gz",
                 max_staging_bytes=0)

    def test_existing_output_raises_and_keeps_original_bytes(self):
        self.write_sources("v1")
        commit = self.commit_all("fixture v1")
        original = b"PRE-EXISTING-OUTPUT-BYTES\n"
        self.output.write_bytes(original)

        with self.assertRaises(FileExistsError):
            pack(self.repo, commit, self.output)

        self.assertEqual(self.output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
