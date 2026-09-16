"""Exercise a packaged executable or container against a synthetic backup."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from tests.fixture import DEMO_KEY, build_demo_archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--binary", type=Path)
    target.add_argument("--image")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary).resolve()
        build_demo_archive(work)
        if args.image:
            command = [
                "docker", "run", "--rm", "--network=none", "--read-only",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--mount", f"type=bind,source={work},target=/work",
                "--workdir", "/work", args.image,
            ]
        else:
            command = [str(args.binary.resolve())]
        env = os.environ.copy()
        # Run away from the source tree without Python import-path overrides.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.pop("SIGNAL_BACKUP_KEY", None)
        for arguments in (
            ["--help"],
            ["verify", "SignalBackups", "--key", DEMO_KEY, "--deep"],
            ["export", "SignalBackups", "--key", DEMO_KEY, "-m", "media", "-o", "out.json"],
        ):
            subprocess.run(command + arguments, cwd=work, env=env, check=True)

        data = json.loads((work / "out.json").read_text(encoding="utf-8"))
        if len(data["messages"]) != 7:
            raise AssertionError("Expected seven exported messages")
        media = sorted(p.read_bytes() for p in (work / "media").rglob("*") if p.is_file())
        expected = sorted([
            b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 200,
            b"a longer note, stored as a file\n" * 40,
        ])
        if media != expected:
            raise AssertionError("Exported attachments differ from the fixture")


if __name__ == "__main__":
    main()
