# Vendored schemas

These two files are copied **verbatim** from Signal's own repositories so they
can be diffed against upstream. Do not reformat, reorder or hand-edit them.

| File | Upstream | Path | Commit |
| --- | --- | --- | --- |
| `backup.proto` | [libsignal](https://github.com/signalapp/libsignal) | `rust/message-backup/src/proto/backup.proto` | `cb9887dbcab5e098f43c038d2f3eb8998fe196a0` (2026-08-24) |
| `local_archive.proto` | [Signal-Android](https://github.com/signalapp/Signal-Android) | `lib/archive/src/main/protowire/LocalArchive.proto` | `8a887b65a1adc3b87279b21f3f2fe5cd01912925` (2026-03-25) |

Both are © Signal Messenger, LLC and licensed AGPL-3.0-only. See the top-level
`NOTICE`.

## Refreshing them

There is no build step — `signalbackup/protoschema.py` parses these at runtime —
so updating to a newer Signal release is a file copy:

```console
$ curl -o backup.proto \
    https://raw.githubusercontent.com/signalapp/libsignal/main/rust/message-backup/src/proto/backup.proto
$ curl -o local_archive.proto \
    https://raw.githubusercontent.com/signalapp/Signal-Android/main/lib/archive/src/main/protowire/LocalArchive.proto
$ python3 -m unittest discover -s ../../tests -t ../..
```

Then update the commit hashes in the table above and in `NOTICE`.

Unknown wire fields are skipped rather than treated as errors, so a backup
written by a newer Signal than the vendored schema still reads — you just won't
see the new fields until you refresh. Field *removals* upstream are the case to
watch for, and the test suite is what will catch them.
