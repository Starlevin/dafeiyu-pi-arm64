import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest
import zipfile


OPS = runpy.run_path(str(Path(__file__).with_name("wechat-swift-memory-backup.py")))
JOURNAL = "unified-pilot-journal.json"
SCOPE = "a" * 64
STATE = f"wechat-group-memory/{SCOPE}/state.json"
SEGMENT = f"wechat-group-memory/{SCOPE}/journal/seg-000001.log"


class SwiftMemoryBackupTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.source = self.root / "data-qq"
        self.output = self.root / "checkpoint.zip"
        self.destination = self.root / "restored"
        self.files = {
            JOURNAL: b'{"version":1,"entries":[{"state":"done"}]}',
            STATE: json.dumps({"v": 1, "scope": SCOPE}).encode(),
            STATE + ".bak": json.dumps({"v": 1, "scope": SCOPE}).encode(),
            SEGMENT: b'{"seq":1}\n',
        }
        for name, content in self.files.items():
            member = self.source / name
            member.parent.mkdir(parents=True, exist_ok=True)
            member.write_bytes(content)
        (self.source / "unrelated.json").write_text("Exclude adjacent data", encoding="utf-8")
        (self.source / SEGMENT).with_name(".lock").touch()

    def test_roundtrip_preserves_bytes_and_excludes_adjacent_files(self):
        summary = OPS["create"](self.source, self.output)
        self.assertEqual({"files": 4, "journalEntries": 1, "scopes": 1}, summary)
        self.assertEqual(summary, OPS["restore"](self.output, self.destination))
        restored = {p.relative_to(self.destination).as_posix(): p.read_bytes()
                    for p in self.destination.rglob("*") if p.is_file()}
        self.assertEqual(self.files, restored)

    def test_existing_backup_and_destination_are_preserved(self):
        OPS["create"](self.source, self.output)
        original = self.output.read_bytes()
        with self.assertRaises(FileExistsError):
            OPS["create"](self.source, self.output)
        self.assertEqual(original, self.output.read_bytes())
        OPS["restore"](self.output, self.destination)
        with self.assertRaisesRegex(ValueError, "new restore directory"):
            OPS["restore"](self.output, self.destination)
        self.assertEqual(self.files[JOURNAL], (self.destination / JOURNAL).read_bytes())

    def write_archive(self, files, checksum_source=None):
        hashes = {name: hashlib.sha256(content).hexdigest()
                  for name, content in (checksum_source or files).items()}
        with zipfile.ZipFile(self.output, "w") as bundle:
            bundle.writestr("checkpoint.json", json.dumps({"version": 1, "sha256": hashes}))
            for name, content in files.items():
                bundle.writestr(name, content)

    def test_checksum_failure_leaves_destination_absent(self):
        self.write_archive({**self.files, SEGMENT: b"modified"}, checksum_source=self.files)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            OPS["restore"](self.output, self.destination)
        self.assertFalse(self.destination.exists())

    def test_group_state_cannot_be_put_in_another_scope(self):
        self.write_archive({**self.files, STATE: json.dumps({"v": 1, "scope": "b" * 64}).encode()})
        with self.assertRaisesRegex(ValueError, "scope mismatch"):
            OPS["restore"](self.output, self.destination)
        self.assertFalse(self.destination.exists())

    def test_archive_rejects_path_traversal(self):
        self.write_archive({**self.files, "../outside.json": b"{}"})
        with self.assertRaisesRegex(ValueError, "Unexpected file"):
            OPS["restore"](self.output, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / "outside.json").exists())


if __name__ == "__main__":
    unittest.main()
