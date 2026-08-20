"""Command-line interface for reading Signal v2 backup folders."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from . import crypto
from .archive import Archive, ArchiveError, Snapshot, format_backup_id
from .filters import ChatFilter, FilterError, MessageFilter, parse_timestamp
from .media import MediaExtractor
from .model import (
    FRAME_KINDS,
    BackupReader,
    Index,
    build_index,
    iso_timestamp,
    message_attachments,
    message_to_json,
)

PROGRAM = "sigbackup"
ENV_KEY = "SIGNAL_BACKUP_KEY"


class UsageError(Exception):
    """A problem with the invocation, reported without a traceback."""


# --------------------------------------------------------------------------
# Argument plumbing
# --------------------------------------------------------------------------

def _add_archive_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "backup", metavar="BACKUP_DIR",
        help="the SignalBackups folder (or a single signal-backup-* snapshot inside it)",
    )


def _add_key_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("backup key")
    group.add_argument(
        "-k", "--key", metavar="AEP",
        help=f"the 64-character backup key shown in Signal (default: ${ENV_KEY}, else prompt)",
    )
    group.add_argument(
        "--key-file", metavar="PATH", type=Path,
        help="read the backup key from a file instead of the command line",
    )
    group.add_argument(
        "--backup-key-hex", metavar="HEX",
        help="use a pre-derived 32-byte backup key instead of an account entropy pool",
    )
    group.add_argument(
        "--aci", metavar="UUID",
        help="derive the backup ID from this account ACI instead of reading it "
             "from the snapshot metadata",
    )
    group.add_argument(
        "-s", "--snapshot", metavar="NAME",
        help="which snapshot to read (name or fragment); default is the newest complete one",
    )
    group.add_argument(
        "--no-verify-mac", action="store_true",
        help="skip HMAC authentication (faster, but corruption goes unnoticed)",
    )


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("filters")
    group.add_argument(
        "-c", "--chat", action="append", default=[], metavar="SELECTOR",
        help="select a chat; repeatable. A bare value matches the chat id or a "
             "substring of its name; identifiers must match in full. Prefixes: "
             "id:, recipient:, group:, contact:, aci:, e164:",
    )
    group.add_argument("--groups", action="store_true", help="only group chats")
    group.add_argument("--dms", action="store_true", help="only 1:1 chats and Note to Self")
    group.add_argument("--since", metavar="WHEN",
                       help="only messages at or after this time (date, ISO-8601, or epoch ms)")
    group.add_argument("--until", metavar="WHEN", help="only messages at or before this time")
    group.add_argument("--search", metavar="TEXT", help="only messages whose body contains TEXT")
    group.add_argument("--no-updates", action="store_true",
                       help="drop system/update messages (joins, timer changes, calls)")
    group.add_argument("--limit", type=int, metavar="N", help="stop after N messages")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Extract messages and media from a Signal v2 backup folder.",
        epilog=(
            "The backup key is the 64-character recovery key Signal shows under "
            "Settings > Chats > Backups. It can also be supplied via the "
            f"{ENV_KEY} environment variable."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snapshots = sub.add_parser("snapshots", help="list the snapshots in a backup folder")
    _add_archive_args(snapshots)
    snapshots.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    snapshots.set_defaults(func=cmd_snapshots)

    info = sub.add_parser("info", help="summarise a snapshot")
    _add_archive_args(info)
    _add_key_args(info)
    info.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    info.set_defaults(func=cmd_info)

    chats = sub.add_parser("chats", help="list chats, groups and threads")
    _add_archive_args(chats)
    _add_key_args(chats)
    _add_filter_args(chats)
    chats.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    chats.add_argument("--no-counts", action="store_true",
                       help="skip counting messages (much faster on large backups)")
    chats.set_defaults(func=cmd_chats)

    recipients = sub.add_parser("recipients", help="list contacts, groups and other recipients")
    _add_archive_args(recipients)
    _add_key_args(recipients)
    recipients.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    recipients.set_defaults(func=cmd_recipients)

    export = sub.add_parser("export", help="export messages as JSON, optionally with media")
    _add_archive_args(export)
    _add_key_args(export)
    _add_filter_args(export)
    export.add_argument("-o", "--output", metavar="PATH", default="-",
                        help="write here instead of stdout")
    export.add_argument("-f", "--format", choices=("json", "jsonl"), default="json",
                        help="one document (json, buffered in memory) or one record "
                             "per line (jsonl, streamed - prefer it for large backups)")
    export.add_argument("--pretty", action="store_true", help="indent JSON output")
    export.add_argument("-m", "--media", metavar="DIR", type=Path,
                        help="also decrypt attachments into DIR")
    export.add_argument("--media-layout", choices=("by-chat", "flat"), default="by-chat",
                        help="directory structure for extracted media")
    export.add_argument("--overwrite", action="store_true",
                        help="rewrite media files that already exist")
    export.add_argument("--raw", action="store_true",
                        help="include the raw decoded protobuf under each message's 'raw' key")
    export.add_argument("--include-keys", action="store_true",
                        help="include per-attachment decryption keys in the output (sensitive)")
    export.set_defaults(func=cmd_export)

    media = sub.add_parser("media", help="decrypt attachments without exporting messages")
    _add_archive_args(media)
    _add_key_args(media)
    _add_filter_args(media)
    media.add_argument("-o", "--output", metavar="DIR", type=Path, required=True,
                       help="directory to write attachments into")
    media.add_argument("--media-layout", choices=("by-chat", "flat"), default="by-chat",
                       help="directory structure for extracted media")
    media.add_argument("--overwrite", action="store_true",
                       help="rewrite media files that already exist")
    media.add_argument("--manifest", metavar="PATH", type=Path,
                       help="also write a JSON manifest describing every file written")
    media.set_defaults(func=cmd_media)

    verify = sub.add_parser("verify", help="check that a snapshot and its media decrypt cleanly")
    _add_archive_args(verify)
    _add_key_args(verify)
    verify.add_argument("--deep", action="store_true",
                        help="also authenticate every referenced attachment blob")
    verify.set_defaults(func=cmd_verify)

    frames = sub.add_parser("frames", help="dump raw decoded frames as JSON lines (debugging)")
    _add_archive_args(frames)
    _add_key_args(frames)
    frames.add_argument("-o", "--output", metavar="PATH", default="-")
    frames.add_argument("--kind", action="append", default=[], metavar="KIND",
                        choices=FRAME_KINDS,
                        help="only frames of this kind; one of " + ", ".join(FRAME_KINDS))
    frames.add_argument("--limit", type=int, metavar="N")
    frames.set_defaults(func=cmd_frames)

    return parser


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _read_key(args: argparse.Namespace) -> str:
    if args.key:
        return args.key
    if args.key_file:
        try:
            return args.key_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise UsageError(f"could not read key file: {error}") from None
    from_env = os.environ.get(ENV_KEY)
    if from_env:
        return from_env
    if sys.stdin.isatty():
        return getpass.getpass("Signal backup key: ")
    raise UsageError(
        f"no backup key given; pass --key, --key-file, or set ${ENV_KEY}"
    )


def _resolve_keys(args: argparse.Namespace, snapshot: Snapshot) -> crypto.BackupKeys:
    if args.backup_key_hex:
        try:
            backup_key = bytes.fromhex(args.backup_key_hex.strip())
        except ValueError:
            raise UsageError("--backup-key-hex must be hexadecimal") from None
        if len(backup_key) != 32:
            raise UsageError("--backup-key-hex must decode to exactly 32 bytes")
    else:
        try:
            backup_key = crypto.derive_backup_key(_read_key(args))
        except crypto.AccountEntropyPoolError as error:
            raise UsageError(str(error)) from None

    if args.aci:
        try:
            backup_id = crypto.derive_backup_id(backup_key, uuid.UUID(args.aci).bytes)
        except ValueError:
            raise UsageError(f"--aci must be a UUID, got {args.aci!r}") from None
    else:
        backup_id = snapshot.backup_id(backup_key)

    return crypto.derive_message_backup_secrets(backup_key, backup_id)


def _open_archive(args: argparse.Namespace) -> Archive:
    return Archive.open(args.backup)


def _open_reader(args: argparse.Namespace) -> tuple[Archive, Snapshot, BackupReader]:
    archive = _open_archive(args)
    snapshot = archive.snapshot(getattr(args, "snapshot", None))
    if not snapshot.main_path.is_file():
        raise UsageError(f"snapshot {snapshot.name} has no 'main' archive file")
    keys = _resolve_keys(args, snapshot)
    reader = BackupReader(snapshot, keys, verify_mac=not args.no_verify_mac)
    return archive, snapshot, reader


def _chat_filter(args: argparse.Namespace) -> ChatFilter:
    return ChatFilter(
        selectors=list(getattr(args, "chat", []) or []),
        groups_only=getattr(args, "groups", False),
        dms_only=getattr(args, "dms", False),
    )


def _message_filter(args: argparse.Namespace) -> MessageFilter:
    return MessageFilter(
        since_ms=parse_timestamp(args.since) if getattr(args, "since", None) else None,
        until_ms=parse_timestamp(args.until) if getattr(args, "until", None) else None,
        include_updates=not getattr(args, "no_updates", False),
        search=getattr(args, "search", None),
    )


@contextlib.contextmanager
def _output(path: str | Path) -> Iterator[TextIO]:
    if str(path) == "-":
        yield sys.stdout
        return
    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        yield handle


def _dump(value: Any, stream: TextIO, pretty: bool = True) -> None:
    json.dump(value, stream, indent=2 if pretty else None, ensure_ascii=False)
    stream.write("\n")


def _table(rows: list[list[str]], headers: list[str], stream: TextIO | None = None) -> None:
    stream = stream if stream is not None else sys.stdout
    if not rows:
        stream.write("(nothing to show)\n")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for column, cell in enumerate(row):
            widths[column] = max(widths[column], len(cell))
    line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    stream.write(line.rstrip() + "\n")
    stream.write("  ".join("-" * width for width in widths) + "\n")
    for row in rows:
        stream.write("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() + "\n")


def _relative_to(path: Path, root: Path) -> str:
    """A path relative to the media root, falling back to absolute."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_snapshots(args: argparse.Namespace) -> int:
    archive = _open_archive(args)
    snapshots = archive.snapshots()
    media_count = sum(1 for _ in archive.iter_media_files())

    if args.json:
        _dump({
            "root": str(archive.root),
            "mediaFiles": media_count,
            "snapshots": [
                {
                    "name": snapshot.name,
                    "takenAt": snapshot.taken_at.isoformat() if snapshot.taken_at else None,
                    "complete": snapshot.is_complete,
                    "mainBytes": snapshot.size_bytes,
                }
                for snapshot in snapshots
            ],
        }, sys.stdout)
        return 0

    print(f"Archive: {archive.root}")
    print(f"Media blobs in files/: {media_count}")
    print()
    _table(
        [
            [
                snapshot.name,
                snapshot.taken_at.strftime("%Y-%m-%d %H:%M:%S UTC") if snapshot.taken_at else "?",
                _human_bytes(snapshot.size_bytes),
                "yes" if snapshot.is_complete else "INCOMPLETE",
            ]
            for snapshot in snapshots
        ],
        ["SNAPSHOT", "TAKEN", "MAIN SIZE", "COMPLETE"],
    )
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    archive, snapshot, reader = _open_reader(args)
    index = build_index(reader, count_messages=True)
    total_messages = sum(chat.message_count for chat in index.chats.values())
    metadata = snapshot.read_metadata()

    summary = {
        "archiveRoot": str(archive.root),
        "snapshot": snapshot.name,
        "snapshotTakenAt": snapshot.taken_at.isoformat() if snapshot.taken_at else None,
        "localBackupVersion": metadata.get("version", 0),
        "backupId": format_backup_id(reader.keys.backup_id),
        "backupFormatVersion": reader.header.get("version"),
        "backupTimeMs": reader.header.get("backupTimeMs"),
        "backupTimeIso": iso_timestamp(reader.header.get("backupTimeMs")),
        "createdByAppVersion": reader.header.get("currentAppVersion"),
        "firstAppVersion": reader.header.get("firstAppVersion"),
        "recipients": len(index.recipients),
        "chats": len(index.chats),
        "groups": sum(1 for r in index.recipients.values() if r.kind == "group"),
        "contacts": sum(1 for r in index.recipients.values() if r.kind == "contact"),
        "messages": total_messages,
        "mediaFilesOnDisk": sum(1 for _ in archive.iter_media_files()),
        "mediaNamesInSnapshot": len(snapshot.media_names()),
    }

    if args.json:
        _dump(summary, sys.stdout)
        return 0

    width = max(len(key) for key in summary)
    for key, value in summary.items():
        print(f"{key.ljust(width)}  {value if value is not None else '-'}")
    return 0


def cmd_chats(args: argparse.Namespace) -> int:
    _, _, reader = _open_reader(args)
    index = build_index(reader, count_messages=not args.no_counts)
    chat_filter = _chat_filter(args)
    chats = [chat for chat in index.chats.values() if chat_filter.matches(chat)]
    chats.sort(key=lambda chat: (-chat.message_count, chat.name.casefold()))

    if args.json:
        _dump([chat.to_json() for chat in chats], sys.stdout)
        return 0

    _table(
        [
            [
                str(chat.id),
                chat.kind,
                chat.name,
                "-" if args.no_counts else str(chat.message_count),
                ",".join(
                    flag for flag, on in (
                        ("archived", chat.raw.get("archived")),
                        ("pinned", chat.raw.get("pinnedOrder") is not None),
                        ("unread", chat.raw.get("markedUnread")),
                    ) if on
                ) or "-",
            ]
            for chat in chats
        ],
        ["ID", "TYPE", "NAME", "MESSAGES", "FLAGS"],
    )
    return 0


def cmd_recipients(args: argparse.Namespace) -> int:
    _, _, reader = _open_reader(args)
    index = build_index(reader)
    recipients = sorted(index.recipients.values(), key=lambda r: (r.kind, r.name.casefold()))

    if args.json:
        _dump([recipient.to_json() for recipient in recipients], sys.stdout)
        return 0

    _table(
        [
            [
                str(recipient.id),
                recipient.kind,
                recipient.name,
                recipient.e164 or "-",
                recipient.aci or recipient.master_key or "-",
            ]
            for recipient in recipients
        ],
        ["ID", "TYPE", "NAME", "PHONE", "ACI / GROUP KEY"],
    )
    return 0


def _stream_messages(reader: BackupReader, args: argparse.Namespace, archive: Archive,
                     extractor: MediaExtractor | None, on_message,
                     index: Index | None = None) -> tuple[Index, int]:
    """Walk the frame stream, rendering and filtering messages as they arrive.

    The caller may pass its own :class:`Index` so it can resolve chats from
    inside ``on_message`` while the stream is still being consumed.
    """
    chat_filter = _chat_filter(args)
    message_filter = _message_filter(args)
    limit = getattr(args, "limit", None)
    index = index if index is not None else Index()
    emitted = 0

    for kind, value in reader.frames():
        if kind == "recipient":
            index.add_recipient(value, reader.schema)
            continue
        if kind == "chat":
            index.add_chat(value)
            continue
        if kind == "account":
            index.account = value
            continue
        if kind != "chatItem":
            continue

        chat = index.chats.get(value.get("chatId", -1))
        if chat is None or not chat_filter.matches(chat):
            continue

        record = message_to_json(
            value, index, reader.schema, archive=archive,
            include_raw=getattr(args, "raw", False),
            include_keys=getattr(args, "include_keys", False),
        )
        if not message_filter.matches(record):
            continue

        if extractor is not None:
            refs = message_attachments(value, reader.schema)
            metas = record.get("attachments", [])
            for position, ref in enumerate(refs):
                written = extractor.extract(ref, record, position)
                if written is not None and position < len(metas):
                    metas[position]["extractedPath"] = _relative_to(written, extractor.out_dir)

        chat.message_count += 1
        emitted += 1
        on_message(record)
        if limit is not None and emitted >= limit:
            break

    return index, emitted


def cmd_export(args: argparse.Namespace) -> int:
    archive, snapshot, reader = _open_reader(args)
    extractor = None
    if args.media:
        extractor = MediaExtractor(
            archive, args.media, layout=args.media_layout,
            overwrite=args.overwrite, verify_mac=not args.no_verify_mac,
        )

    with _output(args.output) as stream:
        if args.format == "jsonl":
            index, count = _export_jsonl(reader, args, archive, extractor, snapshot, stream)
        else:
            index, count = _export_json(reader, args, archive, extractor, snapshot, stream)

    where = "stdout" if str(args.output) == "-" else str(args.output)
    matched = sum(1 for chat in index.chats.values() if _chat_filter(args).matches(chat))
    print(f"Exported {count} messages from {matched} chats to {where}", file=sys.stderr)
    if extractor is not None:
        _report_media(extractor)
    return 0


def _backup_meta(reader: BackupReader, snapshot: Snapshot, archive: Archive,
                 media_root: Path | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "archiveRoot": str(archive.root),
        "snapshot": snapshot.name,
        "snapshotTakenAt": snapshot.taken_at.isoformat() if snapshot.taken_at else None,
        "backupId": format_backup_id(reader.keys.backup_id),
        "backupFormatVersion": reader.header.get("version"),
        "backupTimeMs": reader.header.get("backupTimeMs"),
        "backupTimeIso": iso_timestamp(reader.header.get("backupTimeMs")),
        "createdByAppVersion": reader.header.get("currentAppVersion"),
    }
    # Signal leaves some header fields unset in local backups. Drop them rather
    # than emitting nulls, matching how message records are rendered.
    meta = {key: value for key, value in meta.items() if value is not None}
    if media_root is not None:
        meta["mediaRoot"] = str(media_root)
    return meta


def _export_json(reader: BackupReader, args: argparse.Namespace, archive: Archive,
                 extractor: MediaExtractor | None, snapshot: Snapshot,
                 stream: TextIO) -> tuple[Index, int]:
    messages: list[dict[str, Any]] = []
    index, count = _stream_messages(reader, args, archive, extractor, messages.append)
    chat_filter = _chat_filter(args)
    document = {
        "backup": _backup_meta(reader, snapshot, archive,
                               extractor.out_dir if extractor else None),
        "recipients": [recipient.to_json() for recipient in index.recipients.values()],
        "chats": [
            chat.to_json() for chat in index.chats.values() if chat_filter.matches(chat)
        ],
        "messages": messages,
    }
    if extractor is not None:
        document["media"] = extractor.stats.to_json()
    _dump(document, stream, pretty=args.pretty)
    return index, count


def _export_jsonl(reader: BackupReader, args: argparse.Namespace, archive: Archive,
                  extractor: MediaExtractor | None, snapshot: Snapshot,
                  stream: TextIO) -> tuple[Index, int]:
    """Stream one tagged JSON object per line: backup, recipient, chat, message.

    Each line carries a ``record`` key naming its kind. That is deliberately not
    ``type``, which payloads use for their own meaning (a chat's "group", a
    message's "standardMessage").

    Recipients and chats are written just before the first message that needs
    them, so a consumer reading the file top-to-bottom never sees a dangling
    reference.
    """
    index = Index()
    seen_recipients: set[int] = set()
    seen_chats: set[int] = set()
    chat_filter = _chat_filter(args)
    header_written = False

    def write(kind: str, payload: dict[str, Any]) -> None:
        stream.write(json.dumps({"record": kind, **payload}, ensure_ascii=False) + "\n")

    def write_recipient(recipient_id: int | None) -> None:
        if recipient_id is None or recipient_id in seen_recipients:
            return
        recipient = index.recipients.get(recipient_id)
        if recipient is not None:
            seen_recipients.add(recipient_id)
            write("recipient", recipient.to_json())

    def on_message(record: dict[str, Any]) -> None:
        nonlocal header_written
        if not header_written:
            write("backup", _backup_meta(reader, snapshot, archive,
                                         extractor.out_dir if extractor else None))
            header_written = True

        chat = index.chats.get(record.get("chatId", -1))
        if chat is not None and chat.id not in seen_chats:
            seen_chats.add(chat.id)
            write_recipient(chat.recipient_id)
            write("chat", chat.to_json())
        write_recipient((record.get("author") or {}).get("id"))
        write("message", record)

    index, count = _stream_messages(reader, args, archive, extractor, on_message, index)

    if not header_written:
        write("backup", _backup_meta(reader, snapshot, archive,
                                     extractor.out_dir if extractor else None))
    for chat in index.chats.values():
        if chat.id not in seen_chats and chat_filter.matches(chat):
            write("chat", chat.to_json())
    return index, count


def cmd_media(args: argparse.Namespace) -> int:
    archive, _, reader = _open_reader(args)
    extractor = MediaExtractor(
        archive, args.output, layout=args.media_layout,
        overwrite=args.overwrite, verify_mac=not args.no_verify_mac,
    )
    manifest: list[dict[str, Any]] = []

    def collect(record: dict[str, Any]) -> None:
        for attachment in record.get("attachments", []):
            if "extractedPath" in attachment:
                manifest.append({
                    "chatId": record.get("chatId"),
                    "chat": record.get("chat"),
                    "dateSent": record.get("dateSent"),
                    "dateSentIso": record.get("dateSentIso"),
                    "author": (record.get("author") or {}).get("name"),
                    **attachment,
                })

    _, count = _stream_messages(reader, args, archive, extractor, collect)
    print(f"Scanned {count} messages", file=sys.stderr)
    _report_media(extractor)

    if args.manifest:
        with _output(args.manifest) as stream:
            _dump({"mediaRoot": str(extractor.out_dir), "files": manifest}, stream)
    return 0


def _report_media(extractor: MediaExtractor) -> None:
    stats = extractor.stats
    print(
        f"Media: {stats.written} written ({_human_bytes(stats.bytes_written)}), "
        f"{stats.reused} already present, {stats.missing} unavailable, "
        f"{stats.failed} failed",
        file=sys.stderr,
    )
    for error in stats.errors[:10]:
        print(f"  ! {error}", file=sys.stderr)
    if len(stats.errors) > 10:
        print(f"  ! ...and {len(stats.errors) - 10} more", file=sys.stderr)


def cmd_verify(args: argparse.Namespace) -> int:
    archive, snapshot, reader = _open_reader(args)
    print(f"Snapshot: {snapshot.name}")

    crypto.verify_backup_mac(snapshot.main_path, reader.keys.hmac_key)
    print("main archive: authenticated")

    referenced: dict[str, tuple[bytes, int | None]] = {}
    frames = 0
    for kind, value in reader.frames():
        frames += 1
        if kind != "chatItem":
            continue
        for ref in message_attachments(value, reader.schema):
            if ref.media_name and ref.local_key:
                referenced[ref.media_name] = (ref.local_key, ref.plaintext_size)
    print(f"frames: {frames} decoded")

    present = [name for name in referenced if archive.has_media(name)]
    missing = [name for name in referenced if name not in present]
    print(f"attachments: {len(referenced)} referenced, {len(present)} present, "
          f"{len(missing)} missing from files/")

    listed = snapshot.media_names()
    if listed:
        on_disk = {name for name in listed if archive.has_media(name)}
        print(f"snapshot file index: {len(listed)} names, {len(on_disk)} present on disk")

    failures = 0
    if args.deep:
        with open(os.devnull, "wb") as sink:
            for name in present:
                local_key, size = referenced[name]
                try:
                    crypto.decrypt_attachment(archive.media_path(name), local_key, size,
                                              destination=sink)
                except Exception as error:  # noqa: BLE001 - report and keep going
                    failures += 1
                    print(f"  ! {name}: {error}")
        print(f"deep check: {len(present) - failures}/{len(present)} attachments decrypted")

    return 1 if (missing or failures) else 0


def cmd_frames(args: argparse.Namespace) -> int:
    _, _, reader = _open_reader(args)
    wanted = set(args.kind or [])
    written = 0
    with _output(args.output) as stream:
        for kind, value in reader.frames():
            if wanted and kind not in wanted:
                continue
            stream.write(json.dumps({"record": kind, kind: value}, ensure_ascii=False) + "\n")
            written += 1
            if args.limit is not None and written >= args.limit:
                break
    print(f"Wrote {written} frames", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (UsageError, ArchiveError, FilterError) as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return 2
    except crypto.MacMismatch as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return 3
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
