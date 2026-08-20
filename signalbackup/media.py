"""Extracting attachment files out of a backup's shared ``files/`` directory."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import crypto
from .archive import Archive
from .model import AttachmentRef

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_EXTRA_TYPES = {
    "audio/aac": ".aac",
    "audio/mp4": ".m4a",
    "image/heic": ".heic",
    "image/webp": ".webp",
    "video/quicktime": ".mov",
    "application/x-signal-plain": ".txt",
}
_MAX_NAME = 80


def slugify(text: str, fallback: str = "unnamed") -> str:
    """A filesystem-safe fragment: no separators, no surprises, not too long."""
    cleaned = _SAFE_CHARS.sub("_", (text or "").strip()).strip("._-")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    return (cleaned[:_MAX_NAME] or fallback)


def guess_extension(content_type: str | None, file_name: str | None) -> str:
    if file_name:
        suffix = Path(file_name).suffix
        if 1 < len(suffix) <= 8:
            return suffix
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _EXTRA_TYPES:
            return _EXTRA_TYPES[base]
        guessed = mimetypes.guess_extension(base)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".bin"


@dataclass
class MediaStats:
    written: int = 0
    reused: int = 0
    missing: int = 0
    failed: int = 0
    bytes_written: int = 0
    missing_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "deduplicated": self.reused,
            "missing": self.missing,
            "failed": self.failed,
            "bytesWritten": self.bytes_written,
        }


class MediaExtractor:
    """Decrypts attachments into an output directory, de-duplicating by media name."""

    def __init__(self, archive: Archive, out_dir: Path, *, layout: str = "by-chat",
                 overwrite: bool = False, verify_mac: bool = True) -> None:
        self.archive = archive
        self.out_dir = Path(out_dir)
        self.layout = layout
        self.overwrite = overwrite
        self.verify_mac = verify_mac
        self.stats = MediaStats()
        self._written: dict[str, Path] = {}

    def extract(self, ref: AttachmentRef, record: dict[str, Any], sequence: int) -> Path | None:
        """Decrypt one attachment; returns where it landed, or None if unavailable."""
        if ref.media_name is None or ref.local_key is None:
            self.stats.missing += 1
            return None

        already = self._written.get(ref.media_name)
        if already is not None:
            self.stats.reused += 1
            return already

        source = self.archive.media_path(ref.media_name)
        if not source.is_file():
            self.stats.missing += 1
            self.stats.missing_names.append(ref.media_name)
            return None

        destination = self._destination(ref, record, sequence)
        if destination.exists() and not self.overwrite:
            self._written[ref.media_name] = destination
            self.stats.reused += 1
            return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        try:
            with open(partial, "wb") as handle:
                crypto.decrypt_attachment(
                    source, ref.local_key, ref.plaintext_size,
                    destination=handle, verify_mac=self.verify_mac,
                )
            partial.replace(destination)
        except Exception as error:  # noqa: BLE001 - one bad file must not stop the export
            partial.unlink(missing_ok=True)
            self.stats.failed += 1
            self.stats.errors.append(f"{ref.media_name}: {error}")
            return None

        self.stats.written += 1
        self.stats.bytes_written += destination.stat().st_size
        self._written[ref.media_name] = destination
        return destination

    def _destination(self, ref: AttachmentRef, record: dict[str, Any], sequence: int) -> Path:
        extension = guess_extension(ref.content_type, ref.file_name)
        if self.layout == "flat":
            return self.out_dir / f"{ref.media_name}{extension}"

        chat_dir = self.out_dir / slugify(
            f"{record.get('chatId', 0):03d}-{record.get('chat') or 'unknown'}", "chat"
        )
        stamp = _stamp(record.get("dateSent"))
        if ref.file_name:
            stem = slugify(Path(ref.file_name).stem, ref.media_name[:12])
        else:
            stem = f"{ref.role}-{ref.media_name[:12]}"
        return chat_dir / f"{stamp}-{sequence:02d}-{stem}{extension}"


def _stamp(millis: int | None) -> str:
    if not millis:
        return "unknown-time"
    try:
        moment = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "unknown-time"
    return moment.strftime("%Y%m%d-%H%M%S")
