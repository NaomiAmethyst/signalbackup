"""Known-answer tests for key derivation, taken from libsignal's own test suite.

The vectors come from ``rust/account-keys/src/backup.rs`` and
``rust/message-backup/src/key.rs``. If these pass, this tool derives exactly the
keys Signal does.
"""

from __future__ import annotations

import os
import unittest
import uuid

from signalbackup import crypto

AEP = "dtjs858asj6tv0jzsqrsmj0ubp335pisj98e9ssnss8myoc08drhtcktyawvx45l"
ACI = uuid.UUID("659aa5f4-a28d-fcc1-1ea1-b997537a3d95")

BACKUP_KEY = bytes.fromhex("ea26a2ddb5dba5ef9e34e1b8dea1f5ae7f255306a6d2d883e542306eaa9fe985")
BACKUP_ID = bytes.fromhex("8a624fbc45379043f39f1391cddc5fe8")
HMAC_KEY = bytes.fromhex("f425e22a607c529717e1e1b29f9fe139f9d1c7e7d01e371c7753c544a3026649")
AES_KEY = bytes.fromhex("e143f4ad5668d8bfed2f88562f0693f53bda2c0e55c9d71730f30e24695fd6df")

# The forward-secrecy variant, used by archive-CDN backups rather than local ones.
FS_TOKEN = bytes.fromhex("69207061737320746865206b6e69666520746f207468652061786f6c6f746c21")
FS_HMAC_KEY = bytes.fromhex("20e6ab57b87e051f3e695e953cf8a261dd307e4f92ae2921673f1d397e07887b")
FS_AES_KEY = bytes.fromhex("602af6ecfc09d695a8d58da9f18225e967979c5e03543ca0224a03cca3d9735e")


class TestKeyDerivation(unittest.TestCase):
    def test_backup_key_from_account_entropy_pool(self):
        self.assertEqual(crypto.derive_backup_key(AEP), BACKUP_KEY)

    def test_backup_id(self):
        self.assertEqual(crypto.derive_backup_id(BACKUP_KEY, ACI.bytes), BACKUP_ID)

    def test_message_backup_key_local(self):
        keys = crypto.derive_message_backup_secrets(BACKUP_KEY, BACKUP_ID)
        self.assertEqual(keys.hmac_key, HMAC_KEY)
        self.assertEqual(keys.aes_key, AES_KEY)

    def test_message_backup_key_with_forward_secrecy(self):
        keys = crypto.derive_message_backup_secrets(BACKUP_KEY, BACKUP_ID, FS_TOKEN)
        self.assertEqual(keys.hmac_key, FS_HMAC_KEY)
        self.assertEqual(keys.aes_key, FS_AES_KEY)

    def test_local_metadata_key_is_stable_and_distinct(self):
        key = crypto.derive_local_metadata_key(BACKUP_KEY)
        self.assertEqual(len(key), 32)
        self.assertNotIn(key, (HMAC_KEY, AES_KEY, BACKUP_KEY))
        self.assertEqual(key, crypto.derive_local_metadata_key(BACKUP_KEY))

    def test_backup_id_rejects_bad_aci(self):
        with self.assertRaises(ValueError):
            crypto.derive_backup_id(BACKUP_KEY, b"short")


class TestAccountEntropyPoolParsing(unittest.TestCase):
    def test_accepts_raw_value(self):
        self.assertEqual(crypto.normalize_account_entropy_pool(AEP), AEP)

    def test_accepts_grouped_uppercase_display_form(self):
        grouped = " ".join(AEP.upper()[i:i + 4] for i in range(0, 64, 4))
        self.assertEqual(crypto.normalize_account_entropy_pool(grouped), AEP)

    def test_undoes_display_substitutions(self):
        # Signal renders 'O' as '#' and '0' as '=' so they cannot be confused.
        source = "o" * 32 + "0" * 32
        display = "#" * 32 + "=" * 32
        self.assertEqual(crypto.normalize_account_entropy_pool(display), source)

    def test_rejects_wrong_length(self):
        with self.assertRaises(crypto.AccountEntropyPoolError):
            crypto.normalize_account_entropy_pool("too short")


class TestAesCtr32(unittest.TestCase):
    def test_round_trip(self):
        key, nonce = os.urandom(32), os.urandom(12)
        plaintext = os.urandom(200)
        ciphertext = crypto.aes256_ctr32(key, nonce, plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(crypto.aes256_ctr32(key, nonce, ciphertext), plaintext)

    def test_counter_block_layout(self):
        # libsignal builds the counter block as nonce || uint32be(counter), so
        # encrypting with counter 1 must equal skipping the first AES block.
        key, nonce = os.urandom(32), os.urandom(12)
        data = os.urandom(32)
        from_zero = crypto.aes256_ctr32(key, nonce, b"\x00" * 16 + data)
        from_one = crypto.aes256_ctr32(key, nonce, data, initial_counter=1)
        self.assertEqual(from_zero[16:], from_one)

    def test_rejects_bad_nonce(self):
        with self.assertRaises(ValueError):
            crypto.aes256_ctr32(os.urandom(32), os.urandom(16), b"data")


class TestPadding(unittest.TestCase):
    def test_minimum_size(self):
        self.assertEqual(crypto.padded_size(0), 541)
        self.assertEqual(crypto.padded_size(1), 541)
        self.assertEqual(crypto.padded_size(541), 541)

    def test_grows_monotonically(self):
        previous = 0
        for size in (600, 1000, 10_000, 1_000_000):
            padded = crypto.padded_size(size)
            self.assertGreaterEqual(padded, size)
            self.assertGreater(padded, previous)
            previous = padded


class TestMediaName(unittest.TestCase):
    def test_matches_signals_definition(self):
        # MediaName.forLocalBackupFilename = hex(SHA-256(plaintextHash || localKey))
        import hashlib
        plaintext_hash, local_key = os.urandom(32), os.urandom(64)
        expected = hashlib.sha256(plaintext_hash + local_key).hexdigest()
        self.assertEqual(crypto.media_name_for(plaintext_hash, local_key), expected)
        self.assertEqual(len(expected), 64)


if __name__ == "__main__":
    unittest.main()
