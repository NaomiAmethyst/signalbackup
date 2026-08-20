"""Builds a synthetic Signal v2 backup folder for tests and demos.

This writes the same bytes Signal Android would: the same directory layout, the
same key derivation, the same AES-CBC + HMAC framing, and attachments encrypted
with the same zero-padded attachment cipher.  If the tool can read what this
produces, it is reading the real format.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import padding as _padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from signalbackup import crypto
from signalbackup.protoschema import length_delimited
from signalbackup.schema import BACKUP_INFO, FRAME, METADATA, load_schema

# A throwaway account entropy pool; this is the value libsignal uses in its own
# tests, so the derived keys are known-good.
DEMO_KEY = "dtjs858asj6tv0jzsqrsmj0ubp335pisj98e9ssnss8myoc08drhtcktyawvx45l"
DEMO_ACI = uuid.UUID("659aa5f4-a28d-fcc1-1ea1-b997537a3d95")


def encrypt_attachment(plaintext: bytes, local_key: bytes) -> bytes:
    """Mirror of Signal's ``AttachmentCipherOutputStream`` over padded input."""
    target = crypto.padded_size(len(plaintext))
    padded = plaintext + b"\x00" * (target - len(plaintext))

    iv = os.urandom(16)
    padder = _padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(local_key[:32]), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padder.update(padded) + padder.finalize())
    ciphertext += encryptor.finalize()

    mac = hmac.new(local_key[32:], iv + ciphertext, hashlib.sha256).digest()
    return iv + ciphertext + mac


def encrypt_main(frames: bytes, keys: crypto.BackupKeys) -> bytes:
    """Gzip, AES-CBC encrypt and authenticate a frame stream."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as archive:
        archive.write(frames)
    compressed = buffer.getvalue()

    iv = os.urandom(16)
    padder = _padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(keys.aes_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padder.update(compressed) + padder.finalize())
    ciphertext += encryptor.finalize()

    body = iv + ciphertext
    return body + hmac.new(keys.hmac_key, body, hashlib.sha256).digest()


@dataclass
class Attachment:
    """One synthetic attachment, with the keys needed to place it in files/."""

    data: bytes
    content_type: str
    file_name: str | None = None

    def __post_init__(self) -> None:
        self.local_key = os.urandom(64)
        self.plaintext_hash = hashlib.sha256(self.data).digest()
        self.media_name = crypto.media_name_for(self.plaintext_hash, self.local_key)

    def pointer(self) -> dict[str, Any]:
        return {
            "contentType": self.content_type,
            **({"fileName": self.file_name} if self.file_name else {}),
            "locatorInfo": {
                "key": os.urandom(64).hex(),
                "plaintextHash": self.plaintext_hash.hex(),
                "size": len(self.data),
                "localKey": self.local_key.hex(),
            },
        }


class BackupBuilder:
    """Assembles frames, then writes a complete archive directory."""

    def __init__(self, account_entropy_pool: str = DEMO_KEY, aci: uuid.UUID = DEMO_ACI,
                 backup_time_ms: int = 1_755_500_000_000,
                 app_version: str | None = "7.99.0") -> None:
        self.schema = load_schema()
        self.backup_key = crypto.derive_backup_key(account_entropy_pool)
        self.backup_id = crypto.derive_backup_id(self.backup_key, aci.bytes)
        self.keys = crypto.derive_message_backup_secrets(self.backup_key, self.backup_id)
        self.backup_time_ms = backup_time_ms
        # Real Signal Android local backups leave the app-version fields unset;
        # pass app_version=None to reproduce that.
        self.app_version = app_version
        self.frames: list[dict[str, Any]] = []
        self.attachments: list[Attachment] = []

    # -- frame construction -------------------------------------------------

    def add_frame(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def add_account(self, profile_name: str = "Me") -> None:
        self.add_frame({"account": {
            "profileKey": os.urandom(32).hex(),
            "givenName": profile_name,
            "accountSettings": {"readReceipts": True},
        }})

    def add_self(self, recipient_id: int) -> int:
        self.add_frame({"recipient": {"id": recipient_id, "self": {}}})
        return recipient_id

    def add_contact(self, recipient_id: int, given: str, family: str = "",
                    e164: int | None = None, aci: uuid.UUID | None = None) -> int:
        contact: dict[str, Any] = {
            "aci": (aci or uuid.uuid4()).bytes.hex(),
            "profileGivenName": given,
            "registered": {},
            "profileSharing": True,
        }
        if family:
            contact["profileFamilyName"] = family
        if e164:
            contact["e164"] = e164
        self.add_frame({"recipient": {"id": recipient_id, "contact": contact}})
        return recipient_id

    def add_group(self, recipient_id: int, title: str, member_acis: list[uuid.UUID]) -> int:
        self.add_frame({"recipient": {"id": recipient_id, "group": {
            "masterKey": os.urandom(32).hex(),
            "whitelisted": True,
            "snapshot": {
                "title": {"title": title},
                "version": 2,
                "members": [
                    {"userId": member.bytes.hex(), "role": "DEFAULT", "joinedAtVersion": 1}
                    for member in member_acis
                ],
            },
        }}})
        return recipient_id

    def add_chat(self, chat_id: int, recipient_id: int, **extra: Any) -> int:
        self.add_frame({"chat": {"id": chat_id, "recipientId": recipient_id, **extra}})
        return chat_id

    def add_message(self, chat_id: int, author_id: int, date_sent: int, body: str | None = None,
                    *, incoming: bool = True, attachments: list[Attachment] | None = None,
                    reactions: list[tuple[str, int, int]] | None = None,
                    quote: dict[str, Any] | None = None) -> None:
        standard: dict[str, Any] = {}
        if body is not None:
            standard["text"] = {"body": body}
        if attachments:
            standard["attachments"] = [
                {"pointer": attachment.pointer(), "flag": "NONE"} for attachment in attachments
            ]
            self.attachments.extend(attachments)
        if reactions:
            standard["reactions"] = [
                {"emoji": emoji, "authorId": author, "sentTimestamp": sent, "sortOrder": sent}
                for emoji, author, sent in reactions
            ]
        if quote:
            standard["quote"] = quote

        details = (
            {"incoming": {"dateReceived": date_sent + 500, "read": True}}
            if incoming else
            {"outgoing": {
                "dateReceived": date_sent,
                "sendStatus": [{"recipientId": author_id, "timestamp": date_sent,
                                "delivered": {"sealedSender": True}}],
            }}
        )
        self.add_frame({"chatItem": {
            "chatId": chat_id,
            "authorId": author_id,
            "dateSent": date_sent,
            **details,
            "standardMessage": standard,
        }})

    def add_update(self, chat_id: int, author_id: int, date_sent: int,
                   simple_type: str = "JOINED_SIGNAL") -> None:
        self.add_frame({"chatItem": {
            "chatId": chat_id,
            "authorId": author_id,
            "dateSent": date_sent,
            "directionless": {},
            "updateMessage": {"simpleUpdate": {"type": simple_type}},
        }})

    # -- writing ------------------------------------------------------------

    def write(self, root: Path, snapshot_name: str = "signal-backup-2026-08-18-04-12-30",
              *, include_media: bool = True) -> Path:
        """Write a ``SignalBackups`` tree under ``root`` and return its path."""
        archive_root = Path(root) / "SignalBackups"
        snapshot_dir = archive_root / snapshot_name
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (archive_root / "files").mkdir(exist_ok=True)
        (archive_root / ".nomedia").touch()

        header_fields: dict[str, Any] = {
            "version": 1,
            "backupTimeMs": self.backup_time_ms,
            "mediaRootBackupKey": os.urandom(32).hex(),
        }
        if self.app_version is not None:
            header_fields["currentAppVersion"] = self.app_version
            header_fields["firstAppVersion"] = "7.50.0"
        header = self.schema.encode(header_fields, BACKUP_INFO, bytes_as="hex")
        records = [header] + [
            self.schema.encode(frame, FRAME, bytes_as="hex") for frame in self.frames
        ]
        (snapshot_dir / "main").write_bytes(
            encrypt_main(length_delimited(records), self.keys)
        )

        metadata_key = crypto.derive_local_metadata_key(self.backup_key)
        nonce = os.urandom(12)
        (snapshot_dir / "metadata").write_bytes(self.schema.encode(
            {"version": 1, "backupId": {
                "iv": nonce.hex(),
                "encryptedId": crypto.aes256_ctr32(metadata_key, nonce, self.backup_id).hex(),
            }},
            METADATA, bytes_as="hex",
        ))

        (snapshot_dir / "files").write_bytes(length_delimited(
            self.schema.encode({"mediaName": attachment.media_name},
                               "signal.backup.local.FilesFrame")
            for attachment in self.attachments
        ))

        if include_media:
            for attachment in self.attachments:
                blob = archive_root / "files" / attachment.media_name[:2] / attachment.media_name
                blob.parent.mkdir(parents=True, exist_ok=True)
                blob.write_bytes(encrypt_attachment(attachment.data, attachment.local_key))

        return archive_root


def build_demo_archive(root: Path, **kwargs: Any) -> Path:
    """A small but representative archive: two chats, media, reactions, updates."""
    builder = BackupBuilder(**kwargs)
    alice = uuid.UUID("11111111-1111-4111-8111-111111111111")
    bob = uuid.UUID("22222222-2222-4222-8222-222222222222")

    builder.add_account()
    me = builder.add_self(1)
    alice_id = builder.add_contact(2, "Alice", "Anderson", 15551230001, alice)
    bob_id = builder.add_contact(3, "Bob", "Brown", 15551230002, bob)
    group_id = builder.add_group(4, "Hiking Club", [alice, bob])

    dm = builder.add_chat(10, alice_id)
    group_chat = builder.add_chat(11, group_id, expirationTimerMs=604800000)
    builder.add_chat(12, me)

    photo = Attachment(b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 200, "image/jpeg", "trailhead.jpg")
    note = Attachment(b"a longer note, stored as a file\n" * 40, "text/plain", "notes.txt")

    base = 1_755_400_000_000
    builder.add_message(dm, alice_id, base, "Morning! Are we still on for Saturday?")
    builder.add_message(dm, me, base + 60_000, "Yes - meeting at the trailhead at 8.",
                        incoming=False, reactions=[("\N{THUMBS UP SIGN}", alice_id, base + 90_000)])
    builder.add_message(dm, alice_id, base + 120_000, "Here's the map.", attachments=[photo])
    builder.add_update(dm, alice_id, base + 150_000, "IDENTITY_UPDATE")

    builder.add_message(group_chat, bob_id, base + 200_000, "Who is bringing the coffee?")
    builder.add_message(group_chat, me, base + 260_000, "I've got it covered.", incoming=False,
                        attachments=[note])
    builder.add_message(group_chat, alice_id, base + 300_000, "See you all there!")

    return builder.write(root, **{})


if __name__ == "__main__":  # pragma: no cover - convenience for manual testing
    import sys
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-backup")
    created = build_demo_archive(target)
    print(f"Wrote {created}")
    print(f"Backup key: {DEMO_KEY}")
