"""Absolute-import entry point for frozen executables."""

from signalbackup.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
