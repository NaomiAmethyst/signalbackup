"""Loads the vendored Signal backup ``.proto`` files once per process."""

from __future__ import annotations

import functools
from pathlib import Path

from .protoschema import Schema, parse_proto

PROTO_DIR = Path(__file__).resolve().parent / "protos"

BACKUP_INFO = "signal.backup.BackupInfo"
FRAME = "signal.backup.Frame"
METADATA = "signal.backup.local.Metadata"
FILES_FRAME = "signal.backup.local.FilesFrame"


@functools.lru_cache(maxsize=1)
def load_schema() -> Schema:
    schema = Schema()
    for name in ("backup.proto", "local_archive.proto"):
        parse_proto((PROTO_DIR / name).read_text(encoding="utf-8"), schema)
    return schema
