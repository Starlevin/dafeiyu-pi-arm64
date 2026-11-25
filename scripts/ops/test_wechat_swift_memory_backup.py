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
POLICY = f"wechat-group-memory/{SCOPE}/recall-policy.json"
SEGMENT = f"wechat-group-memory/{SCOPE}/journal/seg-000001.log"
ARCHIVE = f"wechat-group-memory/{SCOPE}/archive.jsonl"


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
            POLICY: json.dumps({"version": 1, "scope": SCOPE, "pausedTopics": ["group topic"]}).encode(),
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
        self.assertEqual({"files": 5, "journalEntries": 1, "scopes": 1}, summary)
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

    def test_recall_policy_cannot_be_restored_to_a_different_group(self):
        foreign = json.dumps({"version": 1, "scope": "b" * 64, "pausedTopics": ["topic"]}).encode()
        self.write_archive({**self.files, POLICY: foreign})
        with self.assertRaisesRegex(ValueError, "recall policy scope mismatch"):
            OPS["restore"](self.output, self.destination)
        self.assertFalse(self.destination.exists())

    def test_restore_carries_only_new_withdrawals_for_the_same_scope(self):
        older = {**self.files, ARCHIVE: json.dumps({
            "kind": "tombstone", "ids": ["already-withdrawn"], "at": "2026-11-25T00:00:00Z",
        }).encode()}
        other_state = STATE.replace(SCOPE, "b" * 64)
        older[other_state] = json.dumps({"v": 1, "scope": "b" * 64}).encode()
        self.write_archive(older)
        (self.source / STATE).write_bytes(json.dumps({
            "v": 1, "scope": SCOPE, "forgottenIDs": ["already-withdrawn", "from-state"],
            "forgottenSourceIDs": ["state-source"], "items": ["new data must not be copied"],
        }).encode())
        (self.source / (STATE + ".bak")).write_bytes(json.dumps({
            "v": 1, "scope": SCOPE, "forgottenIDs": ["from-backup-state"],
        }).encode())
        lines = [
            {"kind": "tombstone", "ids": ["from-archive"], "forgottenSourceIDs": ["archive-source"]},
            {"kind": "itemVersion", "item": {"id": "legacy", "reason": "forget",
                                              "sourceIDs": ["legacy-source"], "text": "do not copy this body"}},
            {"kind": "itemVersion", "item": {"id": "retained", "reason": "correction"}},
            {"kind": "itemVersion", "item": {"id": "episode", "reason": "forget", "kind": "episode",
                                              "sourceIDs": ["unrelated-source"]}},
        ]
        current_archive = "\n".join(json.dumps(row) for row in lines).encode()
        (self.source / ARCHIVE).write_bytes(current_archive)
        unrelated = self.source / STATE.replace(SCOPE, "c" * 64)
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(json.dumps({"v": 1, "scope": "c" * 64, "forgottenIDs": ["elsewhere"]}).encode())

        result = OPS["restore"](self.output, self.destination, withdrawals_from=self.source)
        self.assertEqual(1, result["withdrawalScopesMerged"])
        self.assertEqual(older[STATE], (self.destination / STATE).read_bytes())
        self.assertEqual(older[other_state], (self.destination / other_state).read_bytes())
        self.assertFalse((self.destination / other_state).with_name("archive.jsonl").exists())
        self.assertFalse((self.destination / unrelated.relative_to(self.source)).exists())
        raw = (self.destination / ARCHIVE).read_bytes()
        self.assertTrue(raw.startswith(older[ARCHIVE] + b"\n"))
        old_record, carried = [json.loads(row) for row in raw.splitlines()]
        self.assertEqual(["already-withdrawn"], old_record["ids"])
        self.assertEqual(["episode", "from-archive", "from-backup-state", "from-state", "legacy"], carried["ids"])
        self.assertEqual(["archive-source", "legacy-source", "state-source"], carried["forgottenSourceIDs"])
        self.assertNotIn(b"do not copy this body", raw)
        self.assertEqual(current_archive, (self.source / ARCHIVE).read_bytes())

    def test_restore_from_newer_checkpoint_keeps_only_markers(self):
        OPS["create"](self.source, self.output)
        with (self.source / ARCHIVE).open("w", encoding="utf-8") as archive:
            json.dump({"kind": "tombstone", "ids": ["late-withdrawal"], "at": "2026-11-26T00:00:00Z"}, archive)
        newer = self.root / "newer.zip"
        OPS["create"](self.source, newer)
        result = OPS["restore"](self.output, self.destination, withdrawals_from=newer)
        self.assertEqual(1, result["withdrawalScopesMerged"])
        self.assertEqual(["late-withdrawal"], json.loads((self.destination / ARCHIVE).read_bytes())["ids"])
        newer_again = self.root / "with-withdrawals.zip"
        OPS["create"](self.destination, newer_again)
        repeat = self.root / "restored-again"
        self.assertEqual(0, OPS["restore"](newer_again, repeat, withdrawals_from=newer)["withdrawalScopesMerged"])
        self.assertEqual((self.destination / ARCHIVE).read_bytes(), (repeat / ARCHIVE).read_bytes())

    def test_invalid_newer_marker_does_not_create_a_partial_restore(self):
        OPS["create"](self.source, self.output)
        (self.source / ARCHIVE).write_bytes(b'{"kind":"tombstone","ids":"bad"}\n')
        invalid = self.root / "invalid.zip"
        OPS["create"](self.source, invalid)
        with self.assertRaisesRegex(ValueError, "Invalid withdrawal markers"):
            OPS["restore"](self.output, self.destination, withdrawals_from=invalid)
        self.assertFalse(self.destination.exists())

    def test_restore_suppresses_the_withdrawn_turn_without_losing_deduplication(self):
        def done(key, scope, source_id):
            return {"key": key, "state": "done",
                    "reply": {"ok": True, "action": "reply", "reply": "fixture reply",
                              "trace": "trace", "memoryReceipt": source_id},
                    "memory": {"sessionDigest": scope, "incoming": "fixture older turn", "confirmedAt": 12}}

        source_id = "fixture-same-receipt"
        match = done("withdrawn", SCOPE, source_id)
        other_group = done("other-group", "b" * 64, source_id)
        another = done("another-turn", SCOPE, "different-receipt")
        entries = [match, other_group, another]
        self.write_archive({
            **self.files,
            JOURNAL: json.dumps({"version": 1, "entries": entries}).encode(),
            ARCHIVE: b'{"kind":"tombstone","forgottenSourceIDs":["fixture-same-receipt"]}\n',
        })
        result = OPS["restore"](self.output, self.destination)
        self.assertEqual(1, result["withdrawnTurnsSuppressed"])
        recovered = json.loads((self.destination / JOURNAL).read_bytes())["entries"]
        self.assertEqual({"key": "withdrawn", "state": "done",
                          "reply": {"ok": True, "action": "ignore", "reason": "memory_withdrawn",
                                    "trace": "trace"}}, recovered[0])
        self.assertEqual([other_group, another], recovered[1:])


if __name__ == "__main__":
    unittest.main()
