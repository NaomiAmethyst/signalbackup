"""End-to-end tests: build a real backup folder, then read it back."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar

from signalbackup import crypto
from signalbackup.archive import Archive, ArchiveError
from signalbackup.cli import main
from signalbackup.filters import ChatFilter, FilterError, parse_timestamp
from signalbackup.model import (
    BackupReader,
    build_index,
    iso_timestamp,
    referenced_recipient_ids,
)
from signalbackup.protoschema import ProtoError, parse_proto
from signalbackup.schema import load_schema

from . import fixture
from .fixture import DEMO_KEY, Attachment, BackupBuilder, build_demo_archive


def run_cli(*args: str) -> tuple[int, str, str]:
    """Invoke the CLI in-process and capture its streams."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class DemoArchiveTestCase(unittest.TestCase):
    """Shares one generated archive across the tests that only read it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = build_demo_archive(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def export(self, *extra: str) -> dict:
        code, out, _ = run_cli("export", str(self.root), "--key", DEMO_KEY, *extra)
        self.assertEqual(code, 0)
        return json.loads(out)


class TestArchiveDiscovery(DemoArchiveTestCase):
    def test_opens_archive_root(self):
        archive = Archive.open(self.root)
        self.assertEqual(archive.root, self.root)
        self.assertEqual(len(archive.snapshots()), 1)

    def test_opens_parent_of_archive_root(self):
        archive = Archive.open(self.root.parent)
        self.assertEqual(archive.root, self.root)

    def test_opens_single_snapshot_directory(self):
        snapshot_dir = next(p for p in self.root.iterdir() if p.name.startswith("signal-backup"))
        archive = Archive.open(snapshot_dir)
        self.assertEqual(archive.root, self.root)
        self.assertEqual([s.name for s in archive.snapshots()], [snapshot_dir.name])

    def test_rejects_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(ArchiveError):
            Archive.open(empty)

    def test_snapshot_timestamp_parsed_from_name(self):
        snapshot = Archive.open(self.root).snapshot()
        self.assertIsNotNone(snapshot.taken_at)
        self.assertEqual(snapshot.taken_at.year, 2026)

    def test_media_files_are_sharded_by_name_prefix(self):
        archive = Archive.open(self.root)
        for path in archive.iter_media_files():
            self.assertEqual(path.parent.name, path.name[:2])


class TestSnapshotKeys(DemoArchiveTestCase):
    def test_backup_id_recovered_from_metadata_without_aci(self):
        archive = Archive.open(self.root)
        snapshot = archive.snapshot()
        backup_key = crypto.derive_backup_key(DEMO_KEY)
        expected = crypto.derive_backup_id(backup_key, fixture.DEMO_ACI.bytes)
        self.assertEqual(snapshot.backup_id(backup_key), expected)

    def test_wrong_key_fails_authentication(self):
        wrong = "a" * 64
        code, _, err = run_cli("info", str(self.root), "--key", wrong)
        self.assertEqual(code, 3)
        self.assertIn("authentication failed", err)

    def test_malformed_key_is_a_usage_error(self):
        code, _, err = run_cli("info", str(self.root), "--key", "nope")
        self.assertEqual(code, 2)
        self.assertIn("64 alphanumeric characters", err)

    def test_aci_override_produces_the_same_keys(self):
        code, out, _ = run_cli("info", str(self.root), "--key", DEMO_KEY,
                               "--aci", str(fixture.DEMO_ACI), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["messages"], 7)


class TestReading(DemoArchiveTestCase):
    def test_index_has_expected_recipients_and_chats(self):
        archive = Archive.open(self.root)
        snapshot = archive.snapshot()
        keys = crypto.derive_message_backup_secrets(
            crypto.derive_backup_key(DEMO_KEY),
            snapshot.backup_id(crypto.derive_backup_key(DEMO_KEY)),
        )
        index = build_index(BackupReader(snapshot, keys), count_messages=True)

        self.assertEqual(len(index.recipients), 4)
        self.assertEqual(len(index.chats), 3)
        self.assertEqual({chat.name for chat in index.chats.values()},
                         {"Alice Anderson", "Hiking Club", "Note to Self"})
        self.assertEqual(sum(chat.message_count for chat in index.chats.values()), 7)

    def test_header_is_decoded(self):
        archive = Archive.open(self.root)
        snapshot = archive.snapshot()
        backup_key = crypto.derive_backup_key(DEMO_KEY)
        keys = crypto.derive_message_backup_secrets(backup_key, snapshot.backup_id(backup_key))
        reader = BackupReader(snapshot, keys)
        list(reader.frames())
        self.assertEqual(reader.header["currentAppVersion"], "7.99.0")

    def test_snapshot_file_index_matches_media_on_disk(self):
        archive = Archive.open(self.root)
        names = archive.snapshot().media_names()
        self.assertEqual(len(names), 2)
        for name in names:
            self.assertTrue(archive.has_media(name))


class TestExport(DemoArchiveTestCase):
    def test_json_export_shape(self):
        document = self.export()
        self.assertEqual(set(document), {"backup", "recipients", "chats", "messages"})
        self.assertEqual(len(document["messages"]), 7)
        self.assertEqual(document["backup"]["backupFormatVersion"], 1)

    def test_messages_carry_resolved_names_and_times(self):
        first = self.export()["messages"][0]
        self.assertEqual(first["chat"], "Alice Anderson")
        self.assertEqual(first["author"]["name"], "Alice Anderson")
        self.assertEqual(first["direction"], "incoming")
        self.assertTrue(first["dateSentIso"].endswith("Z"))
        self.assertIn("Saturday", first["body"])

    def test_outgoing_message_has_send_status(self):
        outgoing = [m for m in self.export()["messages"] if m["direction"] == "outgoing"]
        self.assertTrue(outgoing)
        self.assertEqual(outgoing[0]["sendStatus"][0]["status"], "delivered")

    def test_reactions_are_resolved(self):
        reacted = [m for m in self.export()["messages"] if "reactions" in m]
        self.assertEqual(len(reacted), 1)
        self.assertEqual(reacted[0]["reactions"][0]["emoji"], "\N{THUMBS UP SIGN}")
        self.assertEqual(reacted[0]["reactions"][0]["author"]["name"], "Alice Anderson")

    def test_update_messages_are_summarised(self):
        updates = [m for m in self.export()["messages"] if m["type"] == "update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["update"]["type"], "simpleUpdate")
        self.assertEqual(updates[0]["update"]["text"], "Safety number changed")

    def test_attachment_keys_are_withheld_by_default(self):
        attachments = [
            attachment
            for message in self.export()["messages"]
            for attachment in message.get("attachments", [])
        ]
        self.assertEqual(len(attachments), 2)
        for attachment in attachments:
            self.assertNotIn("localKey", attachment)
            self.assertTrue(attachment["available"])

    def test_include_keys_opt_in(self):
        attachments = [
            attachment
            for message in self.export("--include-keys")["messages"]
            for attachment in message.get("attachments", [])
        ]
        self.assertTrue(all(len(a["localKey"]) == 128 for a in attachments))

    def test_jsonl_records_are_tagged_without_clobbering_payloads(self):
        code, out, _ = run_cli("export", str(self.root), "--key", DEMO_KEY, "-f", "jsonl")
        self.assertEqual(code, 0)
        records = [json.loads(line) for line in out.splitlines()]
        kinds = [record["record"] for record in records]
        self.assertEqual(kinds[0], "backup")
        self.assertEqual(kinds.count("message"), 7)
        # A chat's own "type" (contact/group) must survive the record tag.
        chats = [record for record in records if record["record"] == "chat"]
        self.assertEqual({chat["type"] for chat in chats}, {"contact", "group", "self"})

    def test_jsonl_declares_recipients_before_use(self):
        code, out, _ = run_cli("export", str(self.root), "--key", DEMO_KEY, "-f", "jsonl")
        self.assertEqual(code, 0)
        seen: set[int] = set()
        for line in out.splitlines():
            record = json.loads(line)
            if record["record"] == "recipient":
                seen.add(record["id"])
            elif record["record"] == "message":
                self.assertIn(record["author"]["id"], seen)

    def test_raw_frames_available_for_debugging(self):
        code, out, _ = run_cli("frames", str(self.root), "--key", DEMO_KEY, "--kind", "chat")
        self.assertEqual(code, 0)
        records = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["record"], "chat")


class TestFiltering(DemoArchiveTestCase):
    def test_groups_only(self):
        document = self.export("--groups")
        self.assertEqual({m["chat"] for m in document["messages"]}, {"Hiking Club"})
        self.assertEqual(len(document["chats"]), 1)

    def test_dms_only(self):
        document = self.export("--dms")
        self.assertNotIn("Hiking Club", {m["chat"] for m in document["messages"]})

    def test_select_by_chat_id(self):
        document = self.export("--chat", "11")
        self.assertEqual({m["chatId"] for m in document["messages"]}, {11})

    def test_numeric_selector_does_not_match_inside_a_uuid(self):
        # Alice's ACI is 11111111-1111-...; "--chat 11" must mean chat 11 only.
        document = self.export("--chat", "11")
        self.assertEqual({m["chat"] for m in document["messages"]}, {"Hiking Club"})

    def test_full_aci_matches(self):
        document = self.export("--chat", "aci:11111111-1111-4111-8111-111111111111")
        self.assertEqual({m["chat"] for m in document["messages"]}, {"Alice Anderson"})

    def test_partial_aci_does_not_match(self):
        self.assertEqual(self.export("--chat", "aci:11111111")["messages"], [])

    def test_select_by_name_substring(self):
        document = self.export("--chat", "hiking")
        self.assertEqual({m["chat"] for m in document["messages"]}, {"Hiking Club"})

    def test_select_by_phone_number(self):
        document = self.export("--chat", "e164:+1 555 123 0001")
        self.assertEqual({m["chat"] for m in document["messages"]}, {"Alice Anderson"})

    def test_select_by_group_prefix_ignores_contacts(self):
        document = self.export("--chat", "group:Alice")
        self.assertEqual(document["messages"], [])

    def test_multiple_selectors_are_or_ed(self):
        document = self.export("--chat", "11", "--chat", "Alice")
        self.assertEqual({m["chatId"] for m in document["messages"]}, {10, 11})

    def test_time_bounds(self):
        base = 1_755_400_000_000
        document = self.export("--since", str(base + 200_000))
        self.assertTrue(all(m["dateSent"] >= base + 200_000 for m in document["messages"]))
        self.assertEqual(len(document["messages"]), 3)

    def test_search_body(self):
        document = self.export("--search", "coffee")
        self.assertEqual(len(document["messages"]), 1)

    def test_no_updates(self):
        document = self.export("--no-updates")
        self.assertNotIn("update", {m["type"] for m in document["messages"]})

    def test_limit(self):
        self.assertEqual(len(self.export("--limit", "2")["messages"]), 2)

    def test_empty_filter_matches_everything(self):
        self.assertTrue(ChatFilter().is_empty)

    def test_timestamp_parsing(self):
        self.assertEqual(parse_timestamp("1755400000000"), 1_755_400_000_000)
        self.assertEqual(parse_timestamp("1755400000"), 1_755_400_000_000)
        self.assertEqual(parse_timestamp("2026-01-01"), 1_767_225_600_000)
        with self.assertRaises(FilterError):
            parse_timestamp("not a date")


class TestMediaExtraction(DemoArchiveTestCase):
    def test_media_round_trips_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as out:
            code, _, err = run_cli("media", str(self.root), "--key", DEMO_KEY, "-o", out,
                                   "--manifest", str(Path(out) / "manifest.json"))
            self.assertEqual(code, 0)
            self.assertIn("2 written", err)

            written = {p.name: p.read_bytes() for p in Path(out).rglob("*") if p.is_file()}
            photo = next(v for k, v in written.items() if k.endswith(".jpg"))
            notes = next(v for k, v in written.items() if k.endswith(".txt"))

        # Padding must be stripped back to the original plaintext length.
        self.assertEqual(photo, b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 200)
        self.assertEqual(notes, b"a longer note, stored as a file\n" * 40)

    def test_manifest_lists_every_extracted_file(self):
        with tempfile.TemporaryDirectory() as out:
            manifest_path = Path(out) / "manifest.json"
            run_cli("media", str(self.root), "--key", DEMO_KEY, "-o", out,
                    "--manifest", str(manifest_path))
            manifest = json.loads(manifest_path.read_text())
        self.assertEqual(len(manifest["files"]), 2)
        self.assertTrue(all("extractedPath" in entry for entry in manifest["files"]))

    def test_extraction_respects_chat_filter(self):
        with tempfile.TemporaryDirectory() as out:
            run_cli("media", str(self.root), "--key", DEMO_KEY, "-o", out, "--groups")
            files = [p for p in Path(out).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith(".txt"))

    def test_flat_layout(self):
        with tempfile.TemporaryDirectory() as out:
            run_cli("media", str(self.root), "--key", DEMO_KEY, "-o", out,
                    "--media-layout", "flat")
            files = sorted(p.name for p in Path(out).rglob("*") if p.is_file())
        self.assertTrue(all(len(name.split(".")[0]) == 64 for name in files))

    def test_export_annotates_messages_with_relative_paths(self):
        with tempfile.TemporaryDirectory() as out:
            media_dir = Path(out) / "media"
            code, stdout, _ = run_cli("export", str(self.root), "--key", DEMO_KEY,
                                      "-m", str(media_dir))
            self.assertEqual(code, 0)
            document = json.loads(stdout)
            paths = [
                attachment["extractedPath"]
                for message in document["messages"]
                for attachment in message.get("attachments", [])
            ]
            self.assertEqual(len(paths), 2)
            for relative in paths:
                self.assertFalse(os.path.isabs(relative))
                self.assertTrue((media_dir / relative).is_file())
            self.assertEqual(document["backup"]["mediaRoot"], str(media_dir))

    def test_second_run_does_not_rewrite_existing_files(self):
        with tempfile.TemporaryDirectory() as out:
            run_cli("media", str(self.root), "--key", DEMO_KEY, "-o", out)
            _, _, err = run_cli("media", str(self.root), "--key", DEMO_KEY, "-o", out)
        self.assertIn("0 written", err)
        self.assertIn("2 already present", err)


class TestMissingAndCorruptMedia(unittest.TestCase):
    def test_missing_blob_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            victim = next(Archive.open(root).iter_media_files())
            victim.unlink()

            code, out, err = run_cli("export", str(root), "--key", DEMO_KEY,
                                     "-m", str(Path(tmp) / "media"))
            self.assertEqual(code, 0)
            self.assertIn("1 unavailable", err)
            unavailable = [
                attachment
                for message in json.loads(out)["messages"]
                for attachment in message.get("attachments", [])
                if not attachment["available"]
            ]
            self.assertEqual(len(unavailable), 1)

    def test_corrupt_blob_fails_that_file_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            victim = next(Archive.open(root).iter_media_files())
            data = bytearray(victim.read_bytes())
            data[40] ^= 0xFF
            victim.write_bytes(bytes(data))

            code, _, err = run_cli("media", str(root), "--key", DEMO_KEY,
                                   "-o", str(Path(tmp) / "media"))
            self.assertEqual(code, 0)
            self.assertIn("1 written", err)
            self.assertIn("1 failed", err)

    def test_truncated_main_archive_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            main_file = next(root.glob("signal-backup-*/main"))
            main_file.write_bytes(main_file.read_bytes()[:-64])
            code, _, err = run_cli("info", str(root), "--key", DEMO_KEY)
            self.assertEqual(code, 3)
            self.assertIn("authentication failed", err)


class TestVerify(DemoArchiveTestCase):
    def test_clean_archive_verifies(self):
        code, out, _ = run_cli("verify", str(self.root), "--key", DEMO_KEY, "--deep")
        self.assertEqual(code, 0)
        self.assertIn("main archive: authenticated", out)
        self.assertIn("2/2 attachments decrypted", out)

    def test_missing_media_makes_verify_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            next(Archive.open(root).iter_media_files()).unlink()
            code, out, _ = run_cli("verify", str(root), "--key", DEMO_KEY)
            self.assertEqual(code, 1)
            self.assertIn("1 missing", out)


class TestListings(DemoArchiveTestCase):
    def test_chats_table(self):
        code, out, _ = run_cli("chats", str(self.root), "--key", DEMO_KEY)
        self.assertEqual(code, 0)
        self.assertIn("Hiking Club", out)
        self.assertIn("Note to Self", out)

    def test_chats_json_counts(self):
        code, out, _ = run_cli("chats", str(self.root), "--key", DEMO_KEY, "--json")
        self.assertEqual(code, 0)
        chats = {chat["name"]: chat for chat in json.loads(out)}
        self.assertEqual(chats["Hiking Club"]["messageCount"], 3)
        self.assertEqual(chats["Alice Anderson"]["messageCount"], 4)

    def test_recipients_json(self):
        code, out, _ = run_cli("recipients", str(self.root), "--key", DEMO_KEY, "--json")
        self.assertEqual(code, 0)
        recipients = {r["name"]: r for r in json.loads(out)}
        self.assertEqual(recipients["Alice Anderson"]["e164"], "+15551230001")
        self.assertEqual(recipients["Hiking Club"]["memberCount"], 2)

    def test_snapshots_needs_no_key(self):
        code, out, _ = run_cli("snapshots", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("signal-backup-2026", out)

    def test_key_from_environment(self):
        os.environ["SIGNAL_BACKUP_KEY"] = DEMO_KEY
        try:
            code, out, _ = run_cli("info", str(self.root), "--json")
        finally:
            del os.environ["SIGNAL_BACKUP_KEY"]
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["chats"], 3)


class TestLargerBackup(unittest.TestCase):
    """Exercises the streaming paths with enough data to span buffers."""

    def test_many_messages_and_a_large_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BackupBuilder()
            builder.add_account()
            me = builder.add_self(1)
            other = builder.add_contact(2, "Casey", e164=15550000000)
            chat = builder.add_chat(5, other)

            blob = os.urandom(3 * 1024 * 1024)
            big = Attachment(blob, "application/octet-stream", "big.bin")
            for i in range(2000):
                builder.add_message(chat, other if i % 2 else me, 1_700_000_000_000 + i * 1000,
                                    f"message number {i}", incoming=bool(i % 2))
            builder.add_message(chat, me, 1_700_000_100_000, "big one",
                                incoming=False, attachments=[big])

            root = builder.write(Path(tmp))
            out_dir = Path(tmp) / "media"
            code, stdout, _ = run_cli("export", str(root), "--key", DEMO_KEY,
                                      "-f", "jsonl", "-m", str(out_dir))
            self.assertEqual(code, 0)
            messages = [json.loads(line) for line in stdout.splitlines()
                        if json.loads(line)["record"] == "message"]
            self.assertEqual(len(messages), 2001)
            self.assertEqual(messages[0]["body"], "message number 0")
            self.assertEqual(messages[-2]["body"], "message number 1999")

            extracted = next(p for p in out_dir.rglob("*") if p.is_file())
            self.assertEqual(extracted.read_bytes(), blob)


class TestProtoSchema(unittest.TestCase):
    def test_vendored_schema_parses(self):
        schema = load_schema()
        self.assertIn("signal.backup.Frame", schema.messages)
        self.assertIn("signal.backup.local.Metadata", schema.messages)
        self.assertGreater(len(schema.messages), 100)

    def test_oneof_membership(self):
        schema = load_schema()
        self.assertIn("standardMessage",
                      schema.oneof_fields("signal.backup.ChatItem", "item"))

    def test_scoped_type_resolution(self):
        schema = load_schema()
        self.assertEqual(
            schema.resolve("ChatStyle.CustomChatColor", "signal.backup.AccountData"),
            "signal.backup.ChatStyle.CustomChatColor",
        )

    def test_round_trip_encode_decode(self):
        schema = load_schema()
        frame = {"chat": {"id": 7, "recipientId": 3, "archived": True,
                          "expirationTimerMs": 86_400_000}}
        raw = schema.encode(frame, "signal.backup.Frame", bytes_as="hex")
        self.assertEqual(schema.decode(raw, "signal.backup.Frame", bytes_as="hex"), frame)

    def test_repeated_and_nested_round_trip(self):
        schema = load_schema()
        recipient = {"id": 4, "group": {
            "masterKey": "00" * 32,
            "snapshot": {"title": {"title": "Trip"}, "members": [
                {"userId": uuid.uuid4().bytes.hex(), "role": "ADMINISTRATOR"},
                {"userId": uuid.uuid4().bytes.hex(), "role": "DEFAULT"},
            ]},
        }}
        raw = schema.encode({"recipient": recipient}, "signal.backup.Frame", bytes_as="hex")
        decoded = schema.decode(raw, "signal.backup.Frame", bytes_as="hex")
        self.assertEqual(decoded["recipient"], recipient)

    def test_unknown_fields_are_skipped(self):
        schema = load_schema()
        source = parse_proto("""
            syntax = "proto3";
            package t;
            message Extended { uint64 id = 1; string extra = 900; }
            message Narrow { uint64 id = 1; }
        """)
        raw = source.encode({"id": 5, "extra": "future field"}, "t.Extended")
        self.assertEqual(source.decode(raw, "t.Narrow"), {"id": 5})
        del schema  # only here to prove the vendored schema is untouched

    def test_rejects_unknown_field_on_encode(self):
        schema = load_schema()
        with self.assertRaises(ProtoError):
            schema.encode({"nope": 1}, "signal.backup.Chat")

    def test_enum_values_decode_to_names(self):
        schema = load_schema()
        raw = schema.encode({"flag": "GIF"}, "signal.backup.MessageAttachment")
        self.assertEqual(schema.decode(raw, "signal.backup.MessageAttachment"),
                         {"flag": "GIF"})


if __name__ == "__main__":
    unittest.main()


class TestMultipleSnapshots(unittest.TestCase):
    """Snapshot selection, incomplete snapshots, and media-free backups."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

        older = BackupBuilder()
        older.add_account()
        older.add_self(1)
        contact = older.add_contact(2, "Dana")
        chat = older.add_chat(5, contact)
        older.add_message(chat, contact, 1_700_000_000_000, "older snapshot")
        self.root = older.write(self.tmp, "signal-backup-2026-01-01-00-00-00")

        newer = BackupBuilder()
        newer.add_account()
        newer.add_self(1)
        contact = newer.add_contact(2, "Dana")
        chat = newer.add_chat(5, contact)
        newer.add_message(chat, contact, 1_700_000_000_000, "older snapshot")
        newer.add_message(chat, contact, 1_800_000_000_000, "newer snapshot")
        newer.write(self.tmp, "signal-backup-2026-06-01-00-00-00")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_newest_snapshot_is_the_default(self):
        code, out, _ = run_cli("export", str(self.root), "--key", DEMO_KEY)
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["messages"]), 2)

    def test_older_snapshot_by_name_fragment(self):
        code, out, _ = run_cli("export", str(self.root), "--key", DEMO_KEY, "-s", "2026-01")
        self.assertEqual(code, 0)
        messages = json.loads(out)["messages"]
        self.assertEqual([m["body"] for m in messages], ["older snapshot"])

    def test_ambiguous_fragment_is_rejected(self):
        code, _, err = run_cli("export", str(self.root), "--key", DEMO_KEY, "-s", "2026")
        self.assertEqual(code, 2)
        self.assertIn("matches several snapshots", err)

    def test_unknown_snapshot_lists_the_options(self):
        code, _, err = run_cli("export", str(self.root), "--key", DEMO_KEY, "-s", "2030")
        self.assertEqual(code, 2)
        self.assertIn("signal-backup-2026-06-01-00-00-00", err)

    def test_in_progress_snapshot_is_ignored(self):
        (self.root / "signal-backup-2026-07-01-00-00-00-tmp").mkdir()
        code, out, _ = run_cli("snapshots", str(self.root), "--json")
        self.assertEqual(code, 0)
        names = [s["name"] for s in json.loads(out)["snapshots"]]
        self.assertNotIn("signal-backup-2026-07-01-00-00-00-tmp", names)
        self.assertEqual(len(names), 2)

    def test_backup_without_media_works(self):
        code, out, err = run_cli("export", str(self.root), "--key", DEMO_KEY,
                                 "-m", str(self.tmp / "media"))
        self.assertEqual(code, 0)
        self.assertIn("0 written", err)
        self.assertEqual(json.loads(out)["media"]["written"], 0)

    def test_skipping_mac_verification_still_reads(self):
        code, out, _ = run_cli("export", str(self.root), "--key", DEMO_KEY, "--no-verify-mac")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["messages"]), 2)


class TestHostileInput(unittest.TestCase):
    """Claims made in SECURITY.md, held up by tests."""

    NASTY_NAMES: ClassVar[list[str]] = [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\evil.dll",
        "/absolute/path.txt",
        "....//....//escape.sh",
        "..",
        ".",
        "~/.ssh/authorized_keys",
        "nul.txt",
        "a" * 400 + ".bin",
        "\x00truncated.txt",
        "sp ace/and:colon|pipe.txt",
    ]

    def test_attachment_filenames_cannot_escape_the_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BackupBuilder()
            builder.add_account()
            me = builder.add_self(1)
            other = builder.add_contact(2, "Casey")
            chat = builder.add_chat(5, other)
            for index, name in enumerate(self.NASTY_NAMES):
                builder.add_message(
                    chat, me, 1_700_000_000_000 + index, "payload", incoming=False,
                    attachments=[Attachment(f"file {index}".encode(), "text/plain", name)],
                )
            root = builder.write(Path(tmp))

            out = Path(tmp) / "out"
            code, _, err = run_cli("media", str(root), "--key", DEMO_KEY, "-o", str(out))
            self.assertEqual(code, 0)
            self.assertIn(f"{len(self.NASTY_NAMES)} written", err)

            written = [p for p in out.rglob("*") if p.is_file()]
            self.assertEqual(len(written), len(self.NASTY_NAMES))
            for path in written:
                resolved = path.resolve()
                self.assertTrue(
                    resolved.is_relative_to(out.resolve()),
                    f"{resolved} escaped {out}",
                )
                self.assertNotIn("..", resolved.parts)

    def test_hostile_chat_name_cannot_escape_either(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BackupBuilder()
            builder.add_account()
            me = builder.add_self(1)
            group = builder.add_group(2, "../../../../tmp/pwned", [])
            chat = builder.add_chat(5, group)
            builder.add_message(chat, me, 1_700_000_000_000, "hi", incoming=False,
                                attachments=[Attachment(b"data", "image/png", "x.png")])
            root = builder.write(Path(tmp))

            out = Path(tmp) / "out"
            run_cli("media", str(root), "--key", DEMO_KEY, "-o", str(out))
            written = [p for p in out.rglob("*") if p.is_file()]
            self.assertEqual(len(written), 1)
            self.assertTrue(written[0].resolve().is_relative_to(out.resolve()))

    def test_flat_layout_names_are_pure_hex(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = BackupBuilder()
            builder.add_account()
            me = builder.add_self(1)
            chat = builder.add_chat(5, me)
            builder.add_message(chat, me, 1_700_000_000_000, "hi", incoming=False,
                                attachments=[Attachment(b"data", "text/plain",
                                                        "../../escape.txt")])
            root = builder.write(Path(tmp))
            out = Path(tmp) / "out"
            run_cli("media", str(root), "--key", DEMO_KEY, "-o", str(out),
                    "--media-layout", "flat")
            written = [p for p in out.rglob("*") if p.is_file()]
            self.assertEqual(len(written), 1)
            self.assertRegex(written[0].name, r"^[0-9a-f]{64}\.[A-Za-z0-9]+$")

    def test_frame_length_is_bounded(self):
        # A corrupt length prefix must not make us allocate arbitrarily.
        from signalbackup.crypto import MAX_FRAME_LENGTH, iter_length_delimited
        from signalbackup.protoschema import _write_varint

        oversized = _write_varint(MAX_FRAME_LENGTH + 1) + b"\x00" * 16
        with self.assertRaises(ValueError):
            list(iter_length_delimited([oversized]))

    def test_truncated_frame_stream_is_rejected(self):
        from signalbackup.crypto import iter_length_delimited
        from signalbackup.protoschema import _write_varint

        truncated = _write_varint(100) + b"\x00" * 10
        with self.assertRaises(ValueError):
            list(iter_length_delimited([truncated]))


class TestRealWorldHeader(unittest.TestCase):
    """Signal Android leaves the app-version fields unset in local backups."""

    def _archive(self, tmp: Path) -> Path:
        builder = BackupBuilder(app_version=None)
        builder.add_account()
        me = builder.add_self(1)
        other = builder.add_contact(2, "Rowan", e164=15550001111)
        chat = builder.add_chat(7, other)
        builder.add_message(chat, other, 1_755_400_000_000, "hello")
        builder.add_message(chat, me, 1_755_400_060_000, "hi back", incoming=False)
        return builder.write(tmp)

    def test_info_reports_a_missing_app_version_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(Path(tmp))
            code, out, _ = run_cli("info", str(root), "--key", DEMO_KEY, "--json")
            self.assertEqual(code, 0)
            summary = json.loads(out)
            self.assertIsNone(summary["createdByAppVersion"])
            self.assertEqual(summary["messages"], 2)

    def test_info_table_renders_the_gap_as_a_dash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(Path(tmp))
            code, out, _ = run_cli("info", str(root), "--key", DEMO_KEY)
            self.assertEqual(code, 0)
            line = next(row for row in out.splitlines()
                        if row.startswith("createdByAppVersion"))
            self.assertTrue(line.endswith("-"), line)

    def test_export_omits_the_absent_field_rather_than_nulling_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(Path(tmp))
            code, out, _ = run_cli("export", str(root), "--key", DEMO_KEY)
            self.assertEqual(code, 0)
            backup = json.loads(out)["backup"]
            self.assertNotIn("createdByAppVersion", backup)
            self.assertEqual(len(json.loads(out)["messages"]), 2)


class TestTimestampRendering(unittest.TestCase):
    def test_every_raw_millisecond_field_has_an_iso_twin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            code, out, _ = run_cli("export", str(root), "--key", DEMO_KEY)
            self.assertEqual(code, 0)
            document = json.loads(out)

        backup = document["backup"]
        self.assertEqual(backup["backupTimeIso"], iso_timestamp(backup["backupTimeMs"]))

        for message in document["messages"]:
            self.assertEqual(message["dateSentIso"], iso_timestamp(message["dateSent"]))
            if "dateReceived" in message:
                self.assertEqual(message["dateReceivedIso"],
                                 iso_timestamp(message["dateReceived"]))
            for reaction in message.get("reactions", []):
                self.assertEqual(reaction["sentIso"], iso_timestamp(reaction["sentTimestamp"]))

    def test_iso_timestamps_are_utc_with_milliseconds(self):
        self.assertEqual(iso_timestamp(1_755_500_000_000), "2025-08-18T06:53:20.000Z")
        self.assertIsNone(iso_timestamp(None))

    def test_absurd_timestamps_do_not_raise(self):
        self.assertIsNone(iso_timestamp(10**18))


class TestOutputIsAtomic(unittest.TestCase):
    """A failed export must not damage whatever was already at the destination."""

    SENTINEL = '{"precious": "data"}\n'

    def test_wrong_key_leaves_an_existing_export_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            out = Path(tmp) / "existing.json"
            out.write_text(self.SENTINEL, encoding="utf-8")

            code, _, err = run_cli("export", str(root), "--key", "a" * 64, "-o", str(out))

            self.assertEqual(code, 3)
            self.assertIn("authentication failed", err)
            self.assertEqual(out.read_text(encoding="utf-8"), self.SENTINEL)

    def test_corrupt_archive_leaves_an_existing_export_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            main_file = next(root.glob("signal-backup-*/main"))
            main_file.write_bytes(main_file.read_bytes()[:-64])
            out = Path(tmp) / "existing.json"
            out.write_text(self.SENTINEL, encoding="utf-8")

            code, _, _ = run_cli("export", str(root), "--key", DEMO_KEY, "-o", str(out))

            self.assertEqual(code, 3)
            self.assertEqual(out.read_text(encoding="utf-8"), self.SENTINEL)

    def test_failed_export_leaves_no_temporary_files_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            target = Path(tmp) / "exports"
            run_cli("export", str(root), "--key", "a" * 64, "-o", str(target / "out.json"))
            leftovers = [p.name for p in target.iterdir()] if target.exists() else []
            self.assertEqual(leftovers, [])

    def test_successful_export_still_replaces_the_old_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            out = Path(tmp) / "existing.json"
            out.write_text(self.SENTINEL, encoding="utf-8")

            code, _, _ = run_cli("export", str(root), "--key", DEMO_KEY, "-o", str(out))

            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out.read_text())["messages"]), 7)

    def test_export_is_written_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            out = Path(tmp) / "out.json"
            run_cli("export", str(root), "--key", DEMO_KEY, "-o", str(out))
            self.assertEqual(out.stat().st_mode & 0o077, 0, "export readable by others")


class TestMediaCollisions(unittest.TestCase):
    """Two attachments can share a second, a position and a display filename."""

    def _colliding_archive(self, tmp: Path) -> tuple[Path, bytes, bytes]:
        builder = BackupBuilder()
        builder.add_account()
        builder.add_self(1)
        other = builder.add_contact(2, "Dana")
        chat = builder.add_chat(5, other)
        first, second = b"first payload", b"second payload"
        sent = 1_755_400_000_000
        builder.add_message(chat, other, sent, "one",
                            attachments=[Attachment(first, "text/plain", "same.txt")])
        # 100 ms later: same second, same position, same filename, different bytes.
        builder.add_message(chat, other, sent + 100, "two",
                            attachments=[Attachment(second, "text/plain", "same.txt")])
        return builder.write(tmp), first, second

    def test_distinct_attachments_never_share_a_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, first, second = self._colliding_archive(Path(tmp))
            out = Path(tmp) / "media"
            code, _, err = run_cli("media", str(root), "--key", DEMO_KEY, "-o", str(out))
            self.assertEqual(code, 0)

            written = sorted(p for p in out.rglob("*") if p.is_file())
            self.assertEqual(len(written), 2, [p.name for p in written])
            self.assertEqual({p.read_bytes() for p in written}, {first, second})
            self.assertIn("2 written", err)
            self.assertIn("0 already present", err)

    def test_neither_attachment_is_counted_as_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _ = self._colliding_archive(Path(tmp))
            out = Path(tmp) / "media"
            run_cli("media", str(root), "--key", DEMO_KEY, "-o", str(out),
                    "--manifest", str(Path(tmp) / "m.json"))
            manifest = json.loads((Path(tmp) / "m.json").read_text())
            paths = [entry["extractedPath"] for entry in manifest["files"]]
            self.assertEqual(len(paths), 2)
            self.assertEqual(len(set(paths)), 2, f"manifest points twice at {paths}")

    def test_manifest_paths_match_the_bytes_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, first, second = self._colliding_archive(Path(tmp))
            out = Path(tmp) / "media"
            run_cli("media", str(root), "--key", DEMO_KEY, "-o", str(out),
                    "--manifest", str(Path(tmp) / "m.json"))
            manifest = json.loads((Path(tmp) / "m.json").read_text())

            contents = {(out / entry["extractedPath"]).read_bytes()
                        for entry in manifest["files"]}
            self.assertEqual(contents, {first, second})

    def test_true_duplicates_are_still_deduplicated(self):
        # The same attachment referenced twice must not be written twice.
        with tempfile.TemporaryDirectory() as tmp:
            builder = BackupBuilder()
            builder.add_account()
            builder.add_self(1)
            other = builder.add_contact(2, "Dana")
            chat = builder.add_chat(5, other)
            shared = Attachment(b"one and the same", "text/plain", "shared.txt")
            builder.add_message(chat, other, 1_755_400_000_000, "a", attachments=[shared])
            builder.add_message(chat, other, 1_755_400_050_000, "b", attachments=[shared])
            root = builder.write(Path(tmp))

            out = Path(tmp) / "media"
            _, _, err = run_cli("media", str(root), "--key", DEMO_KEY, "-o", str(out))
            written = [p for p in out.rglob("*") if p.is_file()]
            self.assertEqual(len(written), 1)
            self.assertIn("1 written", err)


class TestJsonlDeclaresEveryReference(unittest.TestCase):
    """The documented contract: no record may reference an undeclared recipient."""

    def _rich_archive(self, tmp: Path) -> Path:
        builder = BackupBuilder()
        builder.add_account()
        me = builder.add_self(1)
        alice = uuid.UUID("11111111-1111-4111-8111-111111111111")
        bob = uuid.UUID("22222222-2222-4222-8222-222222222222")
        carol = uuid.UUID("33333333-3333-4333-8333-333333333333")

        alice_id = builder.add_contact(2, "Alice", aci=alice)
        # Bob, Carol and Dave never author a message or own a chat; they appear
        # only as a reaction author, a quote author, a voter and an admin.
        bob_id = builder.add_contact(3, "Bob", aci=bob)
        carol_id = builder.add_contact(4, "Carol", aci=carol)
        dave_id = builder.add_contact(5, "Dave")
        group = builder.add_group(6, "Crew", [alice, bob, carol])
        chat = builder.add_chat(9, group)

        base = 1_755_400_000_000
        builder.add_message(chat, alice_id, base, "reacted to",
                            reactions=[("\N{THUMBS UP SIGN}", bob_id, base + 1_000)])
        builder.add_message(chat, alice_id, base + 10_000, "quoting", quote={
            "authorId": carol_id,
            "targetSentTimestamp": base - 5_000,
            "text": {"body": "the quoted line"},
            "type": "NORMAL",
        })
        builder.add_message(chat, me, base + 20_000, "sent out", incoming=False)
        builder.add_poll(chat, alice_id, base + 30_000, "Lunch?",
                         [("yes", [bob_id, carol_id]), ("no", [dave_id])])
        builder.add_admin_deleted(chat, alice_id, base + 40_000, admin_id=carol_id)
        return builder.write(tmp)

    def _assert_no_dangling(self, output: str) -> list[dict]:
        declared: set[int] = set()
        records = []
        for line in output.splitlines():
            record = json.loads(line)
            records.append(record)
            kind = record["record"]
            if kind == "recipient":
                declared.add(record["id"])
                continue
            if kind == "chat":
                self.assertIn(record["recipientId"], declared,
                              f"chat {record['id']} references undeclared recipient")
                continue
            if kind == "message":
                for recipient_id in sorted(referenced_recipient_ids(record)):
                    self.assertIn(
                        recipient_id, declared,
                        f"message at {record.get('dateSent')} references "
                        f"undeclared recipient {recipient_id}",
                    )
        return records

    def test_nested_references_are_declared_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._rich_archive(Path(tmp))
            code, out, _ = run_cli("export", str(root), "--key", DEMO_KEY, "-f", "jsonl")
            self.assertEqual(code, 0)
            records = self._assert_no_dangling(out)
            self.assertEqual(sum(1 for r in records if r["record"] == "message"), 5)

    def test_reaction_quote_poll_and_admin_authors_all_appear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._rich_archive(Path(tmp))
            _, out, _ = run_cli("export", str(root), "--key", DEMO_KEY, "-f", "jsonl")
            names = {json.loads(line)["name"]
                     for line in out.splitlines()
                     if json.loads(line)["record"] == "recipient"}
            self.assertLessEqual({"Bob", "Carol", "Dave"}, names)

    def test_send_status_recipients_are_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            _, out, _ = run_cli("export", str(root), "--key", DEMO_KEY, "-f", "jsonl")
            self._assert_no_dangling(out)

    def test_filtered_export_with_no_matching_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            code, out, _ = run_cli("export", str(root), "--key", DEMO_KEY,
                                   "-f", "jsonl", "--search", "NO_SUCH_TEXT")
            self.assertEqual(code, 0)
            records = self._assert_no_dangling(out)
            self.assertEqual(sum(1 for r in records if r["record"] == "message"), 0)
            self.assertGreater(sum(1 for r in records if r["record"] == "chat"), 0)

    def test_collector_ignores_non_recipient_structures(self):
        record = {
            "chatId": 4,
            "author": {"id": 7, "name": "Someone"},
            "attachments": [{"fileName": "x.jpg", "size": 3, "mediaName": "ab"}],
            "sticker": {"packId": "ff", "stickerId": 2},
            "linkPreviews": [{"url": "https://example.invalid", "title": "T"}],
        }
        self.assertEqual(referenced_recipient_ids(record), {7})


class TestLimitValidation(unittest.TestCase):
    def test_limit_one_exports_exactly_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            code, out, _ = run_cli("export", str(root), "--key", DEMO_KEY, "--limit", "1")
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)["messages"]), 1)

    def test_zero_and_negative_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            for value in ("0", "-1", "-5"):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("export", str(root), "--key", DEMO_KEY, "--limit", value)
                self.assertEqual(raised.exception.code, 2, f"--limit {value}")

    def test_non_numeric_limit_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            with self.assertRaises(SystemExit):
                run_cli("export", str(root), "--key", DEMO_KEY, "--limit", "lots")

    def test_frames_limit_is_validated_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_demo_archive(Path(tmp))
            with self.assertRaises(SystemExit):
                run_cli("frames", str(root), "--key", DEMO_KEY, "--limit", "0")
            code, out, _ = run_cli("frames", str(root), "--key", DEMO_KEY, "--limit", "2")
            self.assertEqual(code, 0)
            self.assertEqual(len(out.splitlines()), 2)


class TestSchemaRefresh(unittest.TestCase):
    """Guards the 2026-08-24 libsignal refresh: new notification settings.

    The vendored schema is parsed at runtime, so an upstream addition should
    need no code change. These pin that it actually landed and that the parser
    copes with a newly nested enum.
    """

    def test_new_nested_enum_is_parsed(self):
        schema = load_schema()
        enum = schema.enums["signal.backup.AccountData.AccountSettings.UnreadBadgeType"]
        self.assertEqual(enum.by_number,
                         {0: "UNKNOWN_BADGE_TYPE", 1: "UNREAD_MESSAGES", 2: "UNREAD_CHATS"})

    def test_new_chat_notification_fields_round_trip(self):
        schema = load_schema()
        chat = {
            "id": 3,
            "recipientId": 9,
            "notifyForCallsIfMuted": True,
            "notifyForMentionsIfMuted": False,
            "notifyForRepliesIfMuted": True,
            "showUnreadReminders": False,
        }
        raw = schema.encode({"chat": chat}, "signal.backup.Frame", bytes_as="hex")
        self.assertEqual(schema.decode(raw, "signal.backup.Frame", bytes_as="hex"),
                         {"chat": chat})

    def test_new_account_settings_round_trip(self):
        schema = load_schema()
        settings = {
            "unreadBadgeType": "UNREAD_CHATS",
            "includeMutedChatsInBadge": True,
            "reactionNotifications": False,
            "notifyWhenContactJoins": True,
        }
        raw = schema.encode(settings, "signal.backup.AccountData.AccountSettings")
        self.assertEqual(schema.decode(raw, "signal.backup.AccountData.AccountSettings"),
                         settings)

    def test_a_chat_carrying_the_new_fields_still_exports(self):
        # The curated chat output is a summary, so the new settings should not
        # appear there -- but they must not disturb anything either.
        with tempfile.TemporaryDirectory() as tmp:
            builder = BackupBuilder()
            builder.add_account()
            builder.add_self(1)
            other = builder.add_contact(2, "Wren")
            builder.add_chat(4, other, notifyForCallsIfMuted=True,
                             showUnreadReminders=False)
            builder.add_message(4, other, 1_755_400_000_000, "still fine")
            root = builder.write(Path(tmp))

            code, out, _ = run_cli("export", str(root), "--key", DEMO_KEY)
            self.assertEqual(code, 0)
            document = json.loads(out)
            self.assertEqual(len(document["messages"]), 1)
            self.assertEqual(document["chats"][0]["id"], 4)

    def test_raw_frames_expose_the_new_fields(self):
        # Anything not in the curated output stays reachable via `frames`.
        with tempfile.TemporaryDirectory() as tmp:
            builder = BackupBuilder()
            builder.add_account()
            builder.add_self(1)
            other = builder.add_contact(2, "Wren")
            builder.add_chat(4, other, notifyForCallsIfMuted=True)
            root = builder.write(Path(tmp))

            code, out, _ = run_cli("frames", str(root), "--key", DEMO_KEY, "--kind", "chat")
            self.assertEqual(code, 0)
            frame = json.loads(out.splitlines()[0])
            self.assertIs(frame["chat"]["notifyForCallsIfMuted"], True)
