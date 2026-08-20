#!/usr/bin/env sh
# Compare the vendored .proto schemas against upstream.
#
# Exits 0 if they are identical, 1 if Signal has changed them. Run it locally
# before a release, or let the scheduled CI job tell you.
#
# Usage: tools/check-vendored-protos.sh [--update]
#   --update  overwrite the local copies with upstream and print the new commits

set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
protos="$root/signalbackup/protos"
update=${1:-}
status=0

check() {
    local_file="$protos/$1"
    url="$2"
    name="$1"

    tmp=$(mktemp)
    if ! curl -fsSL --max-time 60 -o "$tmp" "$url"; then
        echo "!! could not fetch $name from $url" >&2
        rm -f "$tmp"
        return 1
    fi

    if diff -q "$local_file" "$tmp" >/dev/null 2>&1; then
        echo "ok       $name is identical to upstream"
    elif [ "$update" = "--update" ]; then
        mv "$tmp" "$local_file"
        echo "updated  $name"
        return 0
    else
        echo "DRIFTED  $name differs from upstream:"
        diff -u "$local_file" "$tmp" | head -60 || true
        status=1
    fi
    rm -f "$tmp"
    return 0
}

check backup.proto \
    "https://raw.githubusercontent.com/signalapp/libsignal/main/rust/message-backup/src/proto/backup.proto"
check local_archive.proto \
    "https://raw.githubusercontent.com/signalapp/Signal-Android/main/lib/archive/src/main/protowire/LocalArchive.proto"

if [ "$update" = "--update" ]; then
    echo
    echo "Now run the tests, then update the commit hashes in NOTICE and"
    echo "signalbackup/protos/README.md:"
    echo
    echo "  https://github.com/signalapp/libsignal/commits/main/rust/message-backup/src/proto/backup.proto"
    echo "  https://github.com/signalapp/Signal-Android/commits/main/lib/archive/src/main/protowire/LocalArchive.proto"
fi

exit $status
