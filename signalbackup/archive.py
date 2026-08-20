"""Navigating the on-disk layout of a Signal v2 backup directory.

Signal Android writes a folder that looks like this::

    SignalBackups/
      .nomedia
      files/                     shared attachment blobs, sharded by name prefix
        00/ 01/ ... ff/
          <64 hex chars>         one encrypted attachment
      signal-backup-2026-08-18-04-12-30/
        metadata                 protobuf: version + encrypted backup ID
        main                     the encrypted, gzipped message archive
        files                    protobuf list of media names this snapshot uses

The ``files/`` directory is shared by every snapshot, so a snapshot is only
meaningful in the context of its parent archive.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import crypto
from .schema import FILES_FRAME, METADATA, load_schema

SNAPSHOT_PREFIX = "signal-backup"
ARCHIVE_DIR_NAME = "SignalBackups"
_SNAPSHOT_RE = re.compile(r"^signal-backup-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})$")


class ArchiveError(Exception):
    """The given path is not a usable Signal backup directory."""


@dataclass(frozen=True)
class Snapshot:
    """One point-in-time backup inside an archive."""

    path: Path
    name: str
    taken_at: datetime | None

    @property
    def main_path(self) -> Path:
        return self.path / "main"

    @property
    def metadata_path(self) -> Path:
        return self.path / "metadata"

    @property
    def files_path(self) -> Path:
        return self.path / "files"

    @property
    def is_complete(self) -> bool:
        return self.main_path.is_file() and self.metadata_path.is_file()

    @property
    def size_bytes(self) -> int:
        return self.main_path.stat().st_size if self.main_path.is_file() else 0

    def read_metadata(self) -> dict:
        """Parse the snapshot's ``metadata`` protobuf."""
        if not self.metadata_path.is_file():
            raise ArchiveError(f"snapshot {self.name} has no metadata file")
        raw = self.metadata_path.read_bytes()
        return load_schema().decode(raw, METADATA, bytes_as="hex")

    def backup_id(self, backup_key: bytes) -> bytes:
        """Recover this snapshot's 16-byte backup ID from its metadata.

        Signal stores the ID encrypted under a key derived from the account
        entropy pool alone, so no ACI is needed to read a local backup.
        """
        metadata = self.read_metadata()
        encrypted = metadata.get("backupId")
        if not encrypted:
            raise ArchiveError(
                f"snapshot {self.name} has no encrypted backup ID; "
                "supply the account ACI with --aci instead"
            )
        iv = bytes.fromhex(encrypted.get("iv", ""))
        ciphertext = bytes.fromhex(encrypted.get("encryptedId", ""))
        if len(iv) != 12 or not ciphertext:
            raise ArchiveError(f"snapshot {self.name} has a malformed backup ID block")
        return crypto.aes256_ctr32(crypto.derive_local_metadata_key(backup_key), iv, ciphertext)

    def media_names(self) -> list[str]:
        """The media names this snapshot references, from its ``files`` index.

        Older snapshots may omit the index; callers should fall back to walking
        attachments in the message stream.
        """
        if not self.files_path.is_file():
            return []
        schema = load_schema()
        names: list[str] = []
        with open(self.files_path, "rb") as handle:
            chunks = iter(lambda: handle.read(1 << 20), b"")
            for record in crypto.iter_length_delimited(chunks):
                frame = schema.decode(record, FILES_FRAME)
                name = frame.get("mediaName")
                if name:
                    names.append(name)
        return names


def _parse_snapshot_time(name: str) -> datetime | None:
    match = _SNAPSHOT_RE.match(name)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


class Archive:
    """A ``SignalBackups`` directory: shared media plus a list of snapshots."""

    def __init__(self, root: Path, only: Snapshot | None = None) -> None:
        self.root = root
        self.files_dir = root / "files"
        self._only = only

    # -- discovery ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path) -> Archive:
        """Accept the archive root, its parent, or a single snapshot directory."""
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise ArchiveError(f"{path} does not exist")
        if not path.is_dir():
            raise ArchiveError(f"{path} is not a directory")

        if cls._looks_like_root(path):
            return cls(path)

        nested = path / ARCHIVE_DIR_NAME
        if cls._looks_like_root(nested):
            return cls(nested)

        if (path / "main").is_file():
            snapshot = Snapshot(path, path.name, _parse_snapshot_time(path.name))
            return cls(path.parent, only=snapshot)

        raise ArchiveError(
            f"{path} is not a Signal backup directory. Point at the folder that "
            f"contains 'files/' and 'signal-backup-*' subdirectories (usually "
            f"named '{ARCHIVE_DIR_NAME}'), or at a single snapshot directory."
        )

    @staticmethod
    def _looks_like_root(path: Path) -> bool:
        if not (path / "files").is_dir():
            return False
        if path.name == ARCHIVE_DIR_NAME:
            return True
        return any(
            child.is_dir() and child.name.startswith(SNAPSHOT_PREFIX)
            for child in path.iterdir()
        )

    # -- snapshots ---------------------------------------------------------

    def snapshots(self) -> list[Snapshot]:
        """Snapshots newest first, skipping in-progress ``-tmp`` directories."""
        if self._only is not None:
            return [self._only]
        found = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or not child.name.startswith(SNAPSHOT_PREFIX):
                continue
            if child.name.endswith("-tmp"):
                continue
            found.append(Snapshot(child, child.name, _parse_snapshot_time(child.name)))
        found.sort(key=lambda s: (s.taken_at or datetime.min.replace(tzinfo=timezone.utc)),
                   reverse=True)
        return found

    def snapshot(self, name: str | None = None) -> Snapshot:
        """Select a snapshot by name (or name fragment); newest one by default."""
        available = self.snapshots()
        if not available:
            raise ArchiveError(f"no snapshots found in {self.root}")
        if name is None:
            usable = [s for s in available if s.is_complete]
            if not usable:
                raise ArchiveError(
                    f"no complete snapshots in {self.root} "
                    f"(found {len(available)} without a main/metadata pair)"
                )
            return usable[0]

        exact = [s for s in available if s.name == name]
        if exact:
            return exact[0]
        partial = [s for s in available if name in s.name]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise ArchiveError(
                f"no snapshot matching {name!r}; available: "
                + ", ".join(s.name for s in available)
            )
        raise ArchiveError(
            f"{name!r} matches several snapshots: " + ", ".join(s.name for s in partial)
        )

    # -- media -------------------------------------------------------------

    def media_path(self, media_name: str) -> Path:
        """Where an attachment with this media name lives in ``files/``."""
        return self.files_dir / media_name[:2] / media_name

    def has_media(self, media_name: str) -> bool:
        return self.media_path(media_name).is_file()

    def iter_media_files(self) -> Iterator[Path]:
        """Every attachment blob present in the shared ``files/`` directory."""
        if not self.files_dir.is_dir():
            return
        for shard in sorted(self.files_dir.iterdir()):
            if shard.is_dir():
                yield from sorted(p for p in shard.iterdir() if p.is_file())


def format_backup_id(backup_id: bytes) -> str:
    return base64.b64encode(backup_id).decode("ascii")


__all__ = ["Archive", "ArchiveError", "Snapshot", "format_backup_id"]
