# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because people pipe this tool's output into other programs, the **JSON output
is treated as part of the public interface**: new keys are a minor release,
renamed or removed keys are a major one.

## [Unreleased]

### Fixed

- A failed export no longer destroys an existing output file. The destination
  was opened (and so truncated) before the archive was authenticated, so a
  wrong key or a corrupt archive would leave a previously good export empty.
  Exports now go to a temporary file and are renamed into place on success.
- Two distinct attachments can no longer collapse onto one output file. In the
  by-chat layout the filename was built from a second-resolution timestamp, a
  per-message position and the display filename, none of which is unique; the
  second attachment was then mistaken for an already-extracted copy, never
  written, and its manifest entry pointed at the first one's bytes. Filenames
  now carry a fragment of the media name.
- JSONL output no longer references recipients it has not declared. Only the
  chat's owner and the message's author were emitted ahead of a message, so
  reaction authors, quote authors, send-status recipients, poll voters and
  `deletedBy` admins could dangle. Chats with no matching messages were also
  written without their recipient.
- `--limit 0` and negative limits exported one message instead of none. The
  limit is now validated as a positive integer at parse time, for both
  `export` and `frames`.

### Changed

- Refreshed the vendored `backup.proto` to libsignal
  `cb9887dbcab5e098f43c038d2f3eb8998fe196a0` (2026-08-24), which adds Signal's
  new notification settings: an `UnreadBadgeType` enum and eight
  `AccountData.AccountSettings` fields, plus four per-`Chat` mute overrides.
  The change is purely additive and needed no code change — backups written
  before or after it read either way. The new fields are reachable through
  `frames` and `export --raw`; the curated `chats` output is unchanged.
- Extracted media filenames in the by-chat layout gain a 12-character media-name
  fragment (`...-trailhead-76014dde9cb8.jpg`), which is what makes them unique.
  The `flat` layout is unchanged.
- Export files are created owner-readable only (0600); they contain plaintext
  messages.

## [0.1.0] - 2026-08-20

First release. Verified against real backups written by Signal Android 8.22.2,
in addition to the synthetic archives the test suite builds.

### Added

- Reads Signal Android's v2 backup folders (`SignalBackups/` with `main`,
  `metadata` and `files`), including multi-snapshot archives and shared media.
- Key handling: account entropy pool to backup key to backup ID to archive
  keys, matching libsignal. The backup ID is recovered from the snapshot's
  `metadata`, so no account ACI is needed; `--aci` remains as a fallback.
- `export` writes messages as a single JSON document or as streamed JSONL,
  resolving recipients, chats, reactions, quotes, edits, send status, polls,
  link previews, contact cards and system messages.
- `media` and `export --media` decrypt attachments, de-duplicated by media
  name, in either a per-chat or flat layout.
- Filtering by chat, group, contact, ACI or phone number, plus `--since`,
  `--until`, `--search`, `--no-updates` and `--limit`.
- `snapshots`, `info`, `chats`, `recipients`, `verify` and `frames` commands.
- The backup schema is parsed from vendored `.proto` files at runtime, so
  tracking a new Signal release is a file copy with no build step. Unknown
  wire fields are skipped rather than treated as errors.

- `backupTimeMs` is paired with a `backupTimeIso` rendering, so every raw
  millisecond field in the output has an ISO-8601 twin.

### Security

- Everything is authenticated before it is decrypted; a wrong key or a
  corrupted archive exits 3 rather than producing output.
- Per-attachment decryption keys are omitted from exports unless
  `--include-keys` is passed.
- Attachment filenames and chat names are sanitised so extracted media cannot
  escape the output directory.

[Unreleased]: https://github.com/NaomiAmethyst/signalbackup/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/NaomiAmethyst/signalbackup/releases/tag/v0.1.0
