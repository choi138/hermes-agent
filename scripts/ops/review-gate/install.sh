#!/bin/sh
# Rendering is a dry run unless --apply is explicitly supplied.
exec /usr/bin/python3 - "$0" "$@" <<'PY'
import argparse
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

source_dir = Path(sys.argv.pop(1)).resolve().parent
parser = argparse.ArgumentParser(description="Render the magi shim; --apply writes with a backup.")
parser.add_argument("--apply", action="store_true")
parser.add_argument("--output", default=str(Path.home() / ".local/bin/magi"))
parser.add_argument("--magi-real-bin", default="/Users/choegeun-won/.local/share/uv/tools/magi/bin/magi")
parser.add_argument("--review-gate", default=str(source_dir / "review_gate.py"))
parser.add_argument("--python", default="/usr/bin/python3")
args = parser.parse_args()


def absolute(value):
    # Keep a final symlink visible so --apply can refuse to overwrite it.
    return Path(os.path.abspath(os.path.expanduser(value)))


def shell_literal_contents(value):
    return str(absolute(value)).replace("'", "'\"'\"'")


values = {"@MAGI_REAL_BIN@": shell_literal_contents(args.magi_real_bin),
          "@REVIEW_GATE@": shell_literal_contents(args.review_gate),
          "@PYTHON@": shell_literal_contents(args.python)}
try:
    template = (source_dir / "magi-shim.sh").read_text()
    rendered = re.sub(r"@(?:MAGI_REAL_BIN|REVIEW_GATE|PYTHON)@",
                      lambda match: values[match.group()], template)
    output = absolute(args.output)
    print("{} {}".format("Write:" if args.apply else "Would write:", output))
    print(rendered, end="")
    if args.apply:
        if output.is_symlink():
            parser.exit(3, "Refusing a symlink destination; choose a regular file.\n")
        backup = output.with_name(output.name + ".review-gate-backup")
        if output.exists() and output.read_text() == rendered:
            parser.exit(0, "Shim already matches.\n")
        if output.exists() and (backup.exists() or backup.is_symlink()):
            parser.exit(3, "Backup already exists; preserve it and choose a new destination.\n")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if output.exists():
            shutil.copy2(str(output), str(backup))
            print("Backup: {}".format(backup), file=sys.stderr)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=str(output.parent),
                                             prefix=".review-gate-", delete=False) as handle:
                temporary = handle.name
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o755)
            os.replace(temporary, str(output))
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
except (OSError, ValueError) as exc:
    parser.exit(3, "install: {}\n".format(exc))
PY
