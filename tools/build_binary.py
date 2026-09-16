"""Build a native executable; optionally package it with licences and a checksum."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", action="store_true", help="Build a directory for the container")
    parser.add_argument("--archive", help="Archive name, e.g. sigbackup-linux-amd64")
    args = parser.parse_args()
    if args.onedir and args.archive:
        parser.error("--archive is only supported for single-file executables")

    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir" if args.onedir else "--onefile",
        "--name", "sigbackup", "--specpath", "build", "--paths", ".",
        "--collect-data", "signalbackup", "tools/entrypoint.py",
    ], check=True)

    if args.archive:
        package = Path("build") / args.archive
        package.mkdir(parents=True, exist_ok=True)
        executable = "sigbackup.exe" if sys.platform == "win32" else "sigbackup"
        sources = (Path("dist") / executable, Path("LICENSE"), Path("NOTICE"), Path("README.md"))
        for source in sources:
            shutil.copy2(source, package / source.name)
        archive = Path(shutil.make_archive(
            str(Path("dist") / args.archive),
            "zip" if sys.platform == "win32" else "gztar",
            root_dir=package.parent, base_dir=package.name,
        ))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_name(archive.name + ".sha256").write_text(
            f"{digest}  {archive.name}\n", encoding="utf-8",
        )


if __name__ == "__main__":
    main()
