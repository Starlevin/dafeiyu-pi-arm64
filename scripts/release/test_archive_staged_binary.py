import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from archive_staged_binary import archive_binary


class ArchiveStagedBinaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.release = Path(directory.name).resolve() / "release"
        self.source = self.release / "staging/example-build/dafeiyu"
        self.source.parent.mkdir(parents=True)
        self.manifest = self.release / "production_manifest.json"
        self.manifest.write_text(json.dumps({"files": []}), encoding="utf-8")
        self.data = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\0" + b"fixture" * 128
        self.source.write_bytes(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.archive = self.source.with_suffix(".gz")

    def execute(self, *, apply=False, digest=None, **options):
        return archive_binary(self.source, len(self.data), digest or self.sha,
                              self.release, apply=apply, **options)

    def test_preview_apply_and_repeated_verification(self):
        self.assertEqual(self.execute(), ("verified", None))
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.execute(apply=True)[0], "archived")
        self.assertFalse(self.source.exists())
        self.assertEqual(gzip.decompress(self.archive.read_bytes()), self.data)
        self.assertEqual(self.execute(apply=True)[0], "already_archived")

    def test_resumes_after_publishing_verified_archive(self):
        self.archive.write_bytes(gzip.compress(self.data))
        self.assertEqual(self.execute(apply=True)[0], "archived")
        self.assertFalse(self.source.exists())
        self.assertEqual(gzip.decompress(self.archive.read_bytes()), self.data)

    def test_invalid_external_hash_leaves_original(self):
        with self.assertRaisesRegex(ValueError, "verified copy"):
            self.execute(apply=True, digest="0" * 64)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertFalse(self.archive.exists())

    def test_running_manifest_reference_leaves_original(self):
        running = self.release / "running.json"
        running.write_text(json.dumps({"files": [{"path": str(self.source)}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "production manifest"):
            self.execute(apply=True, running_manifest=running)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertFalse(self.archive.exists())

    def test_existing_corrupt_archive_leaves_original(self):
        self.archive.write_bytes(gzip.compress(self.data[:-1] + b"!"))
        with self.assertRaisesRegex(ValueError, "verified copy"):
            self.execute(apply=True)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_hardlinked_copy_is_refused(self):
        os.link(self.source, self.source.with_name("other-copy"))
        with self.assertRaisesRegex(ValueError, "unlinked staging copy"):
            self.execute(apply=True)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_only_direct_staging_copy_is_accepted(self):
        outside = self.release / "dafeiyu"
        outside.write_bytes(self.data)
        with self.assertRaisesRegex(ValueError, "direct release staging"):
            archive_binary(outside, len(self.data), self.sha, self.release, apply=True)
        self.assertEqual(outside.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
