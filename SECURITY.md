# Security

## Reporting a vulnerability

Email <naomi@amethyst.name>, or open a private advisory on the GitHub mirror:
<https://github.com/NaomiAmethyst/signalbackup/security/advisories/new>.

Please don't use a public issue for anything that could expose message
contents or keys.

I'll acknowledge within a week and aim to have a fix out within 30 days. Tell
me if you have a disclosure deadline and I'll work to it.

This project has no bug bounty.

## Scope

In scope, roughly in order of how much I care:

- Reading a backup with the **wrong key** producing output instead of an error.
  Everything is authenticated before it is decrypted; a bypass of that is the
  most serious thing that can go wrong here.
- Accepting a **tampered** archive or attachment as genuine.
- Writing decrypted content, or a backup key, somewhere the user didn't ask for
  — including keys landing in exported JSON without `--include-keys`.
- Path traversal when extracting media: an attachment's filename must never
  escape the output directory.
- Two distinct attachments resolving to one output path, which would silently
  drop one of them and misattribute the other.
- A failed run destroying an existing output file.
- Crashes or unbounded memory use on malformed input, past the point of being a
  denial of service against someone processing an untrusted archive.

Out of scope:

- The security of Signal itself, or of the backup format's design. Report those
  to <https://signal.org/bugs/>.
- The fact that exported JSON and extracted media are plaintext. That is the
  point of the tool.
- Passing `--key` on the command line leaking into shell history. It is
  documented; use `--key-file`, `SIGNAL_BACKUP_KEY`, or the prompt.
- `--no-verify-mac` skipping authentication. That is what it says it does.

## Design notes

- **Authenticate, then decrypt.** `main` is HMAC-verified over its whole
  encrypted region before any plaintext is produced, and each attachment blob
  is verified before it is written. A wrong key fails loudly (exit code 3).
- **Keys stay out of the output.** Per-attachment decryption keys are omitted
  from exports unless `--include-keys` is passed.
- **Read-only.** Nothing writes to the backup directory.
- **No destructive failures.** Exports are written to a temporary file and
  renamed over the destination only after they complete, so a wrong key or a
  corrupt archive leaves any existing output untouched. Export files are
  created owner-readable only.
- **Offline.** No network calls at runtime; the only dependency is
  `cryptography`.
- **Bounded reads.** Frame lengths are capped and the archive is streamed, so a
  corrupt length field cannot make the tool allocate arbitrarily.

## Threat model

This tool assumes the backup folder and the key you give it are yours. It is
not hardened against a hostile archive crafted specifically to attack the
person reading it — though the bounds above mean a malformed archive should
error out rather than misbehave. If you process backups you did not create,
run it somewhere disposable.
