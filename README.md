# signalbackup

[![CI](https://github.com/NaomiAmethyst/signalbackup/actions/workflows/ci.yml/badge.svg)](https://github.com/NaomiAmethyst/signalbackup/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Licence: AGPL-3.0-only](https://img.shields.io/badge/licence-AGPL--3.0--only-blue)](LICENSE)

Extract messages and media from **Signal's v2 backups** — the `SignalBackups`
snapshot folder that Signal Android writes for on-device backups — into JSON
that other tools can parse.

This reads the modern folder format (`main` / `metadata` / `files`), not the
old single-file `.backup` export. Everything is done locally; nothing is
uploaded anywhere. Confirmed working on real backups from Signal Android
8.22.2.

```console
$ sigbackup chats ~/SignalBackups
ID  TYPE     NAME            MESSAGES  FLAGS
--  -------  --------------  --------  ------
10  contact  Alice Anderson  8412      pinned
11  group    Hiking Club     2043      -
12  self     Note to Self    91        -

$ sigbackup export ~/SignalBackups --chat "Hiking Club" -o hiking.json -m ./media
Exported 2043 messages from 1 chats to hiking.json
Media: 214 written (411.8 MiB), 0 already present, 3 unavailable, 0 failed
```

## Install

### Standalone binaries

Download the archive for your platform from
[GitHub Releases](https://github.com/NaomiAmethyst/signalbackup/releases), extract
it, and run `sigbackup` (`sigbackup.exe` on Windows). Python is bundled; no
Python installation is required. Archives include `LICENSE`, `NOTICE`, and this
README, with a separate `.sha256` checksum alongside each download.

| Platform | Architectures | Build baseline |
| --- | --- | --- |
| Linux | amd64, arm64 | Ubuntu 22.04 (glibc 2.35 or newer) |
| Windows | amd64 | Windows Server 2022 runner |
| macOS | amd64, arm64 | macOS 15 (Intel), macOS 14 (Apple Silicon) |

Linux binaries target glibc distributions; use the container on Alpine/musl.
macOS binaries are not Developer ID signed or notarized. Builds for a specific
commit are also available under **Artifacts** on its
[CI run](https://github.com/NaomiAmethyst/signalbackup/actions/workflows/ci.yml).

### Container

`ghcr.io/naomiamethyst/signalbackup` supports `linux/amd64` and `linux/arm64`.
The runtime is built `FROM scratch`, with the bundled application and required
musl/zlib libraries, and defaults to a nonroot user. It has no shell or package
manager. The CLI is the entry point, so pass its subcommand directly:

```sh
mkdir -p export
docker run --rm --network none --read-only \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$HOME/SignalBackups,dst=/backup,readonly" \
  --mount "type=bind,src=$HOME/.signal-key,dst=/key,readonly" \
  --mount "type=bind,src=$PWD/export,dst=/output" \
  ghcr.io/naomiamethyst/signalbackup:latest \
  export /backup --key-file /key -o /output/messages.json -m /output/media
```

`latest` and `main` track successful builds from `main`; version tags such as
`v0.1.0` and commit tags (`sha-<full-commit-sha>`) are also published.
Version tags do not move `latest`. For an interactive key prompt, add `-it`.

### Python package

```console
$ git clone https://github.com/NaomiAmethyst/signalbackup
$ cd signalbackup
$ pip install .
```

Requires Python 3.10+ and [`cryptography`](https://pypi.org/project/cryptography/) —
that is the only dependency. There is no protobuf build step: the schema is
parsed from the vendored `.proto` files at runtime.

You can also run it straight from a checkout without installing:

```console
$ python3 -m signalbackup chats ~/SignalBackups
```

## Getting your backup key

Signal shows a 64-character **backup key** (its account entropy pool) under
**Settings → Chats → Backups**. Write it down there; it cannot be recovered
from the backup folder.

Supply it in whichever way suits you:

```console
$ sigbackup info ~/SignalBackups --key "ABCD EFGH ..."   # grouped display form is fine
$ sigbackup info ~/SignalBackups --key-file ~/.signal-key
$ SIGNAL_BACKUP_KEY=... sigbackup info ~/SignalBackups
$ sigbackup info ~/SignalBackups                          # prompts if stdin is a TTY
```

Signal displays `O` as `#` and `0` as `=` so they cannot be confused; both
forms are accepted, as are spaces and letter case.

## What to point it at

Point at the `SignalBackups` folder, its parent, or a single snapshot inside
it. The layout Signal writes:

```
SignalBackups/
  files/                              attachment blobs, shared by all snapshots
    00/ 01/ ... ff/
      <64 hex chars>                  one encrypted attachment
  signal-backup-2026-08-18-04-12-30/  one snapshot
    metadata                          version + encrypted backup ID
    main                              the encrypted, gzipped message archive
    files                             the media names this snapshot uses
```

Because `files/` is shared, always keep a snapshot together with its parent
folder. By default the newest complete snapshot is used; pick another with
`--snapshot`.

## Commands

| Command | What it does |
| --- | --- |
| `snapshots` | List snapshots and media counts. Needs no key. |
| `info` | Summarise one snapshot: versions, counts, backup ID. |
| `chats` | List chats, groups and threads, with message counts. |
| `recipients` | List contacts, groups, and other recipients. |
| `export` | Write messages as JSON or JSONL, optionally extracting media. |
| `media` | Decrypt attachments only, with an optional manifest. |
| `verify` | Authenticate the archive and check every referenced attachment. |
| `frames` | Dump raw decoded protobuf frames as JSON lines (debugging). |

Add `--json` to the listing commands for machine-readable output.

### Filtering

`export`, `media` and `chats` share one set of filters:

```console
# One group, by name
$ sigbackup export ~/SignalBackups --chat "Hiking Club"

# Two chats at once (selectors are OR-ed)
$ sigbackup export ~/SignalBackups --chat 11 --chat "Alice"

# Everything that is a group, or everything that is a 1:1
$ sigbackup export ~/SignalBackups --groups
$ sigbackup export ~/SignalBackups --dms

# By identity
$ sigbackup export ~/SignalBackups --chat "e164:+15551230001"
$ sigbackup export ~/SignalBackups --chat "aci:11111111-1111-4111-8111-111111111111"

# By time and content
$ sigbackup export ~/SignalBackups --since 2026-01-01 --until 2026-06-30
$ sigbackup export ~/SignalBackups --search "trailhead" --no-updates --limit 500
```

A bare `--chat` value matches the chat id (if numeric) or a case-insensitive
substring of the display name. Identifiers — ACI, PNI, username, phone number —
must match in full, so a short selector can't accidentally match part of
someone's UUID. Explicit prefixes are `id:`, `recipient:`, `group:`,
`contact:`, `aci:` and `e164:`; `group:` and `contact:` also restrict the
result to that kind of chat.

`--since` / `--until` accept `YYYY-MM-DD`, an ISO-8601 datetime, or epoch
milliseconds. Times without a zone are read as UTC. `--limit` must be 1 or
greater; zero and negative values are rejected rather than quietly doing
something arbitrary.

### Media

```console
# Extract only the media for one group, plus a manifest
$ sigbackup media ~/SignalBackups --groups -o ./media --manifest media.json
```

Attachments land in
`<out>/<chatid>-<chat-name>/<timestamp>-<n>-<filename>-<mediaid>`, or use
`--media-layout flat` to name every file after its media name. The `<mediaid>`
fragment is the first 12 characters of the attachment's media name: a timestamp
is only second-resolution and `<n>` restarts with every message, so two
attachments sent in the same second under the same display filename would
otherwise collide. Files are de-duplicated by media name (Signal stores one
blob per unique attachment), and re-running skips anything already written
unless you pass `--overwrite`.

Attachments whose blob is missing from `files/` are reported and marked
`"available": false` rather than aborting the export. Signal only stores a blob
locally when the attachment was downloaded on the device.

## Output format

### `--format json` (default)

One document:

```json
{
  "backup":     { "snapshot": "...", "backupTimeMs": 1755500000000, "...": "..." },
  "recipients": [ { "id": 2, "type": "contact", "name": "Alice Anderson", "...": "..." } ],
  "chats":      [ { "id": 11, "type": "group", "name": "Hiking Club", "...": "..." } ],
  "messages":   [ { "chatId": 11, "...": "..." } ]
}
```

### `--format jsonl`

One object per line, each tagged with a `record` key (`backup`, `recipient`,
`chat`, `message`). Recipients and chats are written just before the first
message that references them, so a consumer reading top-to-bottom never sees a
dangling id. The tag is `record` rather than `type` because payloads use `type`
for their own meaning — a chat's `"group"`, a message's `"standardMessage"`.

```console
$ sigbackup export ~/SignalBackups -f jsonl | jq -r 'select(.record=="message") | .body'
```

Every recipient id a record refers to — a chat's owner, a message's author, a
reaction or quote author, a send-status recipient, a poll voter — is declared by
a preceding `recipient` line, so a streaming consumer never meets an id it has
not seen.

### A message

```json
{
  "chatId": 10,
  "chat": "Alice Anderson",
  "chatType": "contact",
  "author": {"id": 2, "name": "Alice Anderson", "aci": "...", "e164": "+15551230001"},
  "direction": "incoming",
  "type": "standardMessage",
  "dateSent": 1755400120000,
  "dateSentIso": "2025-08-17T03:08:40.000Z",
  "read": true,
  "body": "Here's the map.",
  "attachments": [
    {
      "role": "attachment",
      "contentType": "image/jpeg",
      "fileName": "trailhead.jpg",
      "size": 2004,
      "mediaName": "76014dde...",
      "available": true,
      "path": "files/76/76014dde...",
      "extractedPath": "010-Alice_Anderson/20250817-030840-00-trailhead-76014dde9cb8.jpg"
    }
  ]
}
```

Timestamps are Signal's raw milliseconds plus an ISO-8601 UTC rendering. Only
fields that are actually present appear, so absent values are absent rather
than null — Signal leaves `createdByAppVersion` unset in local backups, for
instance, so that key simply won't be there. `type` is the message variant (`standardMessage`, `stickerMessage`,
`update`, `viewOnceMessage`, `poll`, …); system messages carry a rendered
`update.text`. Reactions, quotes, edits (`revisions`), send status, link
previews, polls and contact cards are included where present.

Use `--raw` to attach the fully decoded protobuf under each message's `raw`
key when you need a field this tool doesn't surface, or `frames` to dump the
underlying frames untouched.

## How it works

The format details are taken from Signal-Android and libsignal:

| Step | Detail |
| --- | --- |
| Backup key | `HKDF-SHA256(AEP, info="20240801_SIGNAL_BACKUP_KEY", 32)` |
| Backup ID | `HKDF(backupKey, info="20241024_SIGNAL_BACKUP_ID:" ‖ aci, 16)` — or read from the snapshot's `metadata`, which stores it AES-256-CTR encrypted under `HKDF(backupKey, info="20241011_SIGNAL_LOCAL_BACKUP_METADATA_KEY", 32)` |
| Archive keys | `HKDF(backupKey, info="20241007_SIGNAL_BACKUP_ENCRYPT_MESSAGE_BACKUP:" ‖ backupId, 64)` → HMAC key ‖ AES key |
| `main` | `IV ‖ AES-256-CBC ciphertext ‖ HMAC-SHA256`, wrapping gzip, wrapping varint-length-delimited `BackupInfo` then `Frame` protos |
| Attachments | `IV ‖ AES-256-CBC ciphertext ‖ HMAC-SHA256` under the message's own 64-byte `localKey`, over zero-padded plaintext; stored at `files/<name[:2]>/<name>` where `name = hex(SHA-256(plaintextHash ‖ localKey))` |

Because the backup ID is stored (encrypted) inside each snapshot, reading a
local backup needs only the backup key — no account ACI. `--aci` is available
as a fallback if a snapshot's metadata is damaged.

Everything is authenticated before it is decrypted, so a wrong key or a
corrupted file is reported rather than silently producing garbage. `main` is
streamed, so memory use stays flat regardless of backup size. Because that
authentication happens as the stream is read — after the output file would
normally have been opened — exports are written to a temporary file and renamed
into place only on success, so a failed run cannot destroy a previous export.

The schema lives in `signalbackup/protos/backup.proto`, copied from libsignal.
To follow a newer Signal release, drop in the updated file — there is nothing
to regenerate. Unknown fields are skipped, so a backup written by a newer
Signal than the vendored schema still reads.

## Security notes

- The exported JSON contains your messages in plaintext. Treat the output the
  way you'd treat the backup itself.
- Per-attachment decryption keys are **not** included by default; `--include-keys`
  opts in when you need to decrypt blobs yourself later.
- Passing `--key` on the command line puts it in your shell history. Prefer
  `--key-file`, the `SIGNAL_BACKUP_KEY` environment variable, or the prompt.
- Nothing here talks to the network.

## Development

```console
$ pip install -e '.[dev]'
$ python3 -m unittest discover -s tests -t .
$ ruff check .
```

The tests build a real backup folder — same layout, same key derivation, same
ciphers — and read it back, so the round trip is exercised end to end. The key
derivation tests use libsignal's own published vectors.

Status: confirmed against real backups written by **Signal Android 8.22.2** —
messages and media both came out correct. Beyond that, key derivation is
checked against libsignal's published vectors and the suite round-trips a
byte-identical synthetic archive. If something doesn't parse on your backup,
`verify` and `frames` are the places to start.

To try the CLI against a synthetic archive:

```console
$ python3 -m tests.fixture /tmp/demo
Wrote /tmp/demo/SignalBackups
Backup key: dtjs858asj6tv0jzsqrsmj0ubp335pisj98e9ssnss8myoc08drhtcktyawvx45l

$ sigbackup export /tmp/demo --key dtjs858asj6tv0jzsqrsmj0ubp335pisj98e9ssnss8myoc08drhtcktyawvx45l --pretty
```

### Building distributable artifacts

```sh
python -m pip install . -r tools/requirements-build.txt
python tools/build_binary.py --archive sigbackup-linux-amd64
python -m tools.smoke_test --binary dist/sigbackup

docker build -t signalbackup:local .
python -m tools.smoke_test --image signalbackup:local
```

Build executables on their target OS and architecture; the archive name is just
a label, not a cross-compilation option. Use `dist/sigbackup.exe` on Windows.
The smoke test runs outside the checkout and checks archive authentication,
JSON export, and byte-for-byte media recovery. Container testing also disables
network access and makes the image filesystem read-only.

CI builds native executables and both container architectures on pull requests,
pushes to `main`, `v*` tags, and manual dispatches. Only `main` and `v*` builds in
`NaomiAmethyst/signalbackup` publish to GHCR, using `GITHUB_TOKEN` with
`packages: write`. A `v*` tag also creates a GitHub Release and uploads binary
archives, checksums, and Python distributions after the build jobs pass. Tags
containing a hyphen are marked as prereleases. Pull requests never publish to
the registry or Releases. No extra registry secret is needed; set the GHCR
package visibility to public after its first publication for anonymous pulls.

## Contributing

Bug reports and patches are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
`tests/fixture.py` builds a complete synthetic backup, so you can reproduce
most things without touching real data.

The code lives at <https://code.amethyst.name/naomi/signalbackup>, but
that instance does not take new sign-ups, so **issues and pull requests go to
the GitHub mirror**: <https://github.com/NaomiAmethyst/signalbackup>.
Patches by email to <naomi@amethyst.name> are equally welcome.

**Please never attach a real backup, an export, or your backup key to an
issue.** `sigbackup verify` and `sigbackup frames --limit 5` give the
diagnostic detail that is usually needed instead.

Security issues: see [SECURITY.md](SECURITY.md), and mail
<naomi@amethyst.name> rather than opening a public issue.

## Licence

AGPL-3.0-only — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

`signalbackup/protos/*.proto` are copied verbatim from
[libsignal](https://github.com/signalapp/libsignal) and
[Signal-Android](https://github.com/signalapp/Signal-Android), and the format
handling was written against those same AGPL-3.0-only sources; `NOTICE` records
exactly which files and commits. The `LICENSE`/`NOTICE` pair travels with the
built distributions.

Signal is a registered trademark of Signal Messenger, LLC. This project is
independent and is not affiliated with, endorsed by, or sponsored by Signal
Messenger, LLC.
