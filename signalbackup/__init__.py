"""Read Signal's v2 (snapshot folder) backups: messages, media, and metadata."""

from .archive import Archive, ArchiveError, Snapshot
from .crypto import BackupKeys, MacMismatch, derive_backup_key, derive_message_backup_secrets
from .model import BackupReader, Index, build_index, message_to_json

__version__ = "0.1.0"

__all__ = [
    "Archive",
    "ArchiveError",
    "BackupKeys",
    "BackupReader",
    "Index",
    "MacMismatch",
    "Snapshot",
    "__version__",
    "build_index",
    "derive_backup_key",
    "derive_message_backup_secrets",
    "message_to_json",
]
