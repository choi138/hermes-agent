#!/bin/sh
# Do not create bytecode in the source checkout during read-only checks.
exec /usr/bin/python3 -B "$(dirname "$0")/deploy.py" "$@"
