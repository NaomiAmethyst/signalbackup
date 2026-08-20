# Contributing

Thanks for taking a look. Bug reports and patches are both welcome.

The code lives at <https://code.amethyst.name/naomi/signalbackup>,
mirrored to <https://github.com/NaomiAmethyst/signalbackup>.

**Please file issues and pull requests on GitHub.** The canonical instance is
readable by anyone but does not accept new accounts, so GitHub is where
discussion happens:

- Issues: <https://github.com/NaomiAmethyst/signalbackup/issues>
- Pull requests: <https://github.com/NaomiAmethyst/signalbackup/pulls>

Patches by email to <naomi@amethyst.name> are also fine if you would rather not
use GitHub — `git format-patch` output is welcome.

## Getting set up

```console
$ python3 -m venv .venv && . .venv/bin/activate
$ pip install -e '.[dev]'
$ python -m unittest discover -s tests -t .
$ ruff check .
```

Both must pass before a change lands. CI runs them on Python 3.10 through 3.13.

## Reporting a bug

**Never attach a real backup, an export, or your backup key to an issue.** They
contain your messages, and the key decrypts everything. Instead:

- Say what Signal version wrote the backup, and what this tool printed.
- Run `sigbackup verify <dir> --key ...` and include its output — it says
  whether the archive authenticates and how many attachments resolved.
- If a frame won't parse, `sigbackup frames <dir> --key ... --limit 5` shows
  the decoded structure. Redact before pasting.

If you can, reproduce it against a synthetic archive built by
`tests/fixture.py` and include that as a test case.

## Working on the format

`tests/fixture.py` builds a complete, real backup folder: same layout, same key
derivation, same ciphers. It is the fastest way to reproduce something without
touching anybody's data.

```console
$ python -m tests.fixture /tmp/demo
$ sigbackup export /tmp/demo --key dtjs858asj6tv0jzsqrsmj0ubp335pisj98e9ssnss8myoc08drhtcktyawvx45l --pretty
```

If you extend the fixture to cover a new message type, add the reader side and
a test in the same change.

### Cryptography

Anything touching key derivation must be checked against libsignal's published
test vectors, not just against our own round trip — a round trip will happily
agree with itself while being wrong. `tests/test_crypto.py` shows the pattern.
The upstream vectors live in `rust/account-keys/src/backup.rs` and
`rust/message-backup/src/key.rs`.

Authentication comes before decryption everywhere. Please keep it that way; a
"just skip the MAC" shortcut turns a wrong-key error into silent garbage.

### Updating the vendored schema

```console
$ sh tools/check-vendored-protos.sh            # has Signal changed them?
$ sh tools/check-vendored-protos.sh --update   # take the new copies
```

Then re-run the tests and update the commit hashes in `NOTICE` and
`signalbackup/protos/README.md`. Do not hand-edit the `.proto` files — they are
kept byte-identical to upstream so they stay diffable. A weekly CI job watches
for drift.

## Style

- Ruff enforces the mechanical parts; run it rather than guessing.
- Match the surrounding code. Comments explain *why*, and the format details
  cite the upstream Signal class they mirror — that provenance is the most
  valuable thing in this codebase, so keep adding it.
- New output fields should be additive. People pipe this into other tools, so
  renaming or removing a JSON key is a breaking change; note it in
  `CHANGELOG.md`.

## Licence

By contributing you agree that your contribution is licensed under
AGPL-3.0-only, the same terms as the project. See `LICENSE` and `NOTICE`.
