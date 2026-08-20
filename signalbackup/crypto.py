"""Key derivation and decryption for Signal's backup v2 format.

Everything here mirrors libsignal and Signal-Android:

* ``AccountEntropyPool`` -> backup key -> backup ID -> message backup key,
  matching ``libsignal_account_keys::BackupKey`` and
  ``libsignal::message_backup::MessageBackupKey``.
* The snapshot ``main`` file: an optional forward-secrecy prefix, then
  ``IV || AES-256-CBC ciphertext || HMAC-SHA256``, wrapping gzipped,
  varint-length-delimited protobuf frames (``EncryptedBackupReader``).
* Attachment blobs in ``files/``: ``IV || AES-256-CBC ciphertext || HMAC-SHA256``
  over zero-padded plaintext (``AttachmentCipherOutputStream``).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import struct
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives import padding as _padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "AccountEntropyPoolError",
    "BackupKeys",
    "MacMismatch",
    "aes256_ctr32",
    "decrypt_attachment",
    "derive_backup_id",
    "derive_backup_key",
    "derive_local_metadata_key",
    "derive_message_backup_secrets",
    "iter_length_delimited",
    "media_name_for",
    "normalize_account_entropy_pool",
    "padded_size",
    "read_backup_frames",
    "verify_backup_mac",
]

# Domain separation strings, byte-for-byte from libsignal.
_INFO_BACKUP_KEY = b"20240801_SIGNAL_BACKUP_KEY"
_INFO_BACKUP_ID = b"20241024_SIGNAL_BACKUP_ID:"
_INFO_LOCAL_METADATA_KEY = b"20241011_SIGNAL_LOCAL_BACKUP_METADATA_KEY"
_INFO_MESSAGE_BACKUP = b"20241007_SIGNAL_BACKUP_ENCRYPT_MESSAGE_BACKUP:"
_INFO_MESSAGE_BACKUP_FS = b"20250708_SIGNAL_BACKUP_ENCRYPT_MESSAGE_BACKUP:"

MAGIC_NUMBER = b"SBACKUP\x01"
MAC_SIZE = 32
IV_SIZE = 16
AEP_LENGTH = 64
MAX_FRAME_LENGTH = 25 * 1024 * 1024
_CHUNK = 1 << 20

# Signal shows the backup key with these substitutions so that visually
# ambiguous glyphs are unmistakable; undo them when parsing user input.
_DISPLAY_TO_STORAGE = {"#": "O", "=": "0"}


class AccountEntropyPoolError(ValueError):
    """The supplied backup key is not a well-formed account entropy pool."""


class MacMismatch(Exception):
    """Authentication failed: wrong key, or the file is corrupt/truncated."""


# --------------------------------------------------------------------------
# Key derivation
# --------------------------------------------------------------------------

def hkdf_sha256(ikm: bytes, salt: bytes | None, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt if salt else b"\x00" * 32, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def normalize_account_entropy_pool(value: str) -> str:
    """Turn a user-typed backup key into its 64-character storage form.

    Accepts the grouped, uppercase form Signal displays (including the ``#``
    and ``=`` stand-ins for ``O`` and ``0``) as well as the raw string.
    """
    converted = "".join(_DISPLAY_TO_STORAGE.get(c, c) for c in value.strip())
    stripped = "".join(c for c in converted if c.isascii() and c.isalnum()).lower()
    if len(stripped) != AEP_LENGTH:
        raise AccountEntropyPoolError(
            f"backup key must be {AEP_LENGTH} alphanumeric characters, got {len(stripped)}"
        )
    return stripped


def derive_backup_key(account_entropy_pool: str) -> bytes:
    """The 32-byte root backup key (libsignal ``BackupKey``)."""
    aep = normalize_account_entropy_pool(account_entropy_pool)
    return hkdf_sha256(aep.encode("ascii"), None, _INFO_BACKUP_KEY, 32)


def derive_backup_id(backup_key: bytes, aci: bytes) -> bytes:
    """The 16-byte backup ID, derived from the account's ACI."""
    if len(aci) != 16:
        raise ValueError("ACI must be 16 raw UUID bytes")
    return hkdf_sha256(backup_key, None, _INFO_BACKUP_ID + aci, 16)


def derive_local_metadata_key(backup_key: bytes) -> bytes:
    """AES key protecting the backup ID inside a snapshot's ``metadata`` file."""
    return hkdf_sha256(backup_key, None, _INFO_LOCAL_METADATA_KEY, 32)


@dataclass(frozen=True)
class BackupKeys:
    """The material needed to read one snapshot."""

    backup_key: bytes
    backup_id: bytes
    hmac_key: bytes
    aes_key: bytes


def derive_message_backup_secrets(backup_key: bytes, backup_id: bytes,
                                  forward_secrecy_token: bytes | None = None) -> BackupKeys:
    """Derive the HMAC and AES keys that protect the ``main`` archive.

    Local backups never carry a forward-secrecy token, so they use the original
    domain separator; the parameter exists for archive-CDN backups.
    """
    if forward_secrecy_token is None:
        material = hkdf_sha256(backup_key, None, _INFO_MESSAGE_BACKUP + backup_id, 64)
    else:
        material = hkdf_sha256(backup_key, forward_secrecy_token,
                               _INFO_MESSAGE_BACKUP_FS + backup_id, 64)
    return BackupKeys(backup_key, backup_id, material[:32], material[32:])


def aes256_ctr32(key: bytes, nonce: bytes, data: bytes, initial_counter: int = 0) -> bytes:
    """libsignal's ``Aes256Ctr32``: a 12-byte nonce plus a 32-bit big-endian counter."""
    if len(nonce) != 12:
        raise ValueError("nonce must be 12 bytes")
    iv = nonce + struct.pack(">I", initial_counter)
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return cipher.update(data) + cipher.finalize()


def media_name_for(plaintext_hash: bytes, local_key: bytes) -> str:
    """The ``files/`` filename for an attachment: hex(SHA-256(hash || localKey))."""
    return hashlib.sha256(plaintext_hash + local_key).hexdigest()


def padded_size(size: int) -> int:
    """Signal's attachment padding curve (``PaddingInputStream.getPaddedSize``)."""
    if size <= 0:
        return 541
    return max(541, math.floor(math.pow(1.05, math.ceil(math.log(size) / math.log(1.05)))))


# --------------------------------------------------------------------------
# The snapshot "main" archive
# --------------------------------------------------------------------------

def _read_varint32(stream: BinaryIO) -> int | None:
    """Read a protobuf varint, or return None at a clean end of stream."""
    result = shift = 0
    while shift <= 35:
        byte = stream.read(1)
        if not byte:
            return None
        value = byte[0]
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result
        shift += 7
    raise ValueError("malformed varint")


def _forward_secrecy_prefix_length(stream: BinaryIO) -> int:
    """Size of the optional ``SBACKUP`` forward-secrecy header, or 0 if absent."""
    head = stream.read(len(MAGIC_NUMBER))
    if head != MAGIC_NUMBER:
        return 0
    start = stream.tell()
    length = _read_varint32(stream)
    if length is None or length < 0 or length > 16 * 1024:
        raise ValueError(f"invalid forward secrecy metadata length: {length}")
    varint_len = stream.tell() - start
    return len(MAGIC_NUMBER) + varint_len + length


def _prefix_length(path: Path) -> int:
    with open(path, "rb") as handle:
        return _forward_secrecy_prefix_length(handle)


def verify_backup_mac(path: Path, mac_key: bytes) -> None:
    """Authenticate the whole encrypted region; raises :class:`MacMismatch`."""
    total = path.stat().st_size
    offset = _prefix_length(path)
    body = total - offset
    if body < IV_SIZE + MAC_SIZE:
        raise MacMismatch(f"{path} is too short to be a backup archive")

    digest = hmac.new(mac_key, digestmod=hashlib.sha256)
    remaining = body - MAC_SIZE
    with open(path, "rb") as handle:
        handle.seek(offset)
        while remaining:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                raise MacMismatch(f"{path} ended early while authenticating")
            digest.update(chunk)
            remaining -= len(chunk)
        expected = handle.read(MAC_SIZE)

    if not hmac.compare_digest(digest.digest(), expected):
        raise MacMismatch(
            "backup authentication failed - wrong backup key, or the file is corrupt"
        )


def _decrypted_chunks(path: Path, aes_key: bytes) -> Iterator[bytes]:
    """Yield the decrypted (still gzipped) body of a snapshot ``main`` file."""
    total = path.stat().st_size
    offset = _prefix_length(path)
    with open(path, "rb") as handle:
        handle.seek(offset)
        iv = handle.read(IV_SIZE)
        if len(iv) != IV_SIZE:
            raise ValueError(f"{path} is truncated")

        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
        unpadder = _padding.PKCS7(128).unpadder()
        remaining = total - offset - IV_SIZE - MAC_SIZE
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                raise ValueError(f"{path} is truncated")
            remaining -= len(chunk)
            yield unpadder.update(decryptor.update(chunk))
        yield unpadder.update(decryptor.finalize()) + unpadder.finalize()


def _gunzip(chunks: Iterable[bytes]) -> Iterator[bytes]:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for chunk in chunks:
        if chunk:
            out = decompressor.decompress(chunk)
            if out:
                yield out
    tail = decompressor.flush()
    if tail:
        yield tail


def iter_length_delimited(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Split a byte stream into varint-length-prefixed records."""
    buffer = bytearray()
    source = iter(chunks)
    exhausted = False

    def fill() -> bool:
        nonlocal exhausted
        if exhausted:
            return False
        try:
            buffer.extend(next(source))
        except StopIteration:
            exhausted = True
            return False
        return True

    while True:
        length = header = None
        while True:
            length, header = _peek_varint(buffer)
            if length is not None or not fill():
                break
        if length is None:
            return  # clean end of stream (or a trailing partial varint)
        if length > MAX_FRAME_LENGTH:
            raise ValueError(f"frame length {length} exceeds sanity limit")
        while len(buffer) < header + length:
            if not fill():
                raise ValueError("truncated frame at end of archive")
        yield bytes(buffer[header:header + length])
        del buffer[:header + length]


def _peek_varint(buffer: bytearray) -> tuple[int | None, int]:
    result = shift = 0
    for index, value in enumerate(buffer[:5]):
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result, index + 1
        shift += 7
    return None, 0


def read_backup_frames(path: Path, keys: BackupKeys, *, verify_mac: bool = True) -> Iterator[bytes]:
    """Yield raw frame bytes from a snapshot ``main`` file.

    The first record is a ``BackupInfo``; every record after it is a ``Frame``.
    """
    if verify_mac:
        verify_backup_mac(path, keys.hmac_key)
    yield from iter_length_delimited(_gunzip(_decrypted_chunks(path, keys.aes_key)))


# --------------------------------------------------------------------------
# Attachment blobs
# --------------------------------------------------------------------------

def decrypt_attachment(path: Path, local_key: bytes, plaintext_size: int | None = None,
                       *, destination: BinaryIO | None = None,
                       verify_mac: bool = True) -> bytes | None:
    """Decrypt one file from the archive's ``files/`` directory.

    ``local_key`` is the 64-byte ``FilePointer.LocatorInfo.localKey``: a 32-byte
    AES key followed by a 32-byte HMAC key.  Signal zero-pads attachments before
    encrypting, so the result is truncated back to ``plaintext_size``.

    Writes to ``destination`` if given, otherwise returns the plaintext.
    """
    if len(local_key) != 64:
        raise ValueError(f"local key must be 64 bytes, got {len(local_key)}")
    aes_key, mac_key = local_key[:32], local_key[32:]

    total = path.stat().st_size
    if total < IV_SIZE + MAC_SIZE:
        raise MacMismatch(f"{path} is too short to be an encrypted attachment")

    with open(path, "rb") as handle:
        if verify_mac:
            digest = hmac.new(mac_key, digestmod=hashlib.sha256)
            remaining = total - MAC_SIZE
            while remaining:
                chunk = handle.read(min(_CHUNK, remaining))
                if not chunk:
                    raise MacMismatch(f"{path} ended early while authenticating")
                digest.update(chunk)
                remaining -= len(chunk)
            if not hmac.compare_digest(digest.digest(), handle.read(MAC_SIZE)):
                raise MacMismatch(f"attachment authentication failed for {path.name}")
            handle.seek(0)

        iv = handle.read(IV_SIZE)
        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
        unpadder = _padding.PKCS7(128).unpadder()

        sink = destination if destination is not None else io.BytesIO()
        written = 0
        limit = plaintext_size if plaintext_size is not None else None
        remaining = total - IV_SIZE - MAC_SIZE

        def emit(data: bytes) -> None:
            nonlocal written
            if not data:
                return
            if limit is not None:
                if written >= limit:
                    return
                data = data[:limit - written]
            sink.write(data)
            written += len(data)

        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                raise ValueError(f"{path} is truncated")
            remaining -= len(chunk)
            emit(unpadder.update(decryptor.update(chunk)))
        emit(unpadder.update(decryptor.finalize()) + unpadder.finalize())

    if limit is not None and written < limit:
        raise ValueError(
            f"{path.name} decrypted to {written} bytes, expected {limit}"
        )
    return None if destination is not None else sink.getvalue()
