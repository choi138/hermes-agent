#!/bin/sh
# Local MAGI structured capture; explicit legacy override is preserved.
export MAGI_CODEX_CAPTURE="${MAGI_CODEX_CAPTURE-json-v1}"
if ! command -v codex >/dev/null 2>&1; then
    export PATH="/Users/choegeun-won/.asdf/installs/nodejs/22.19.0/bin:$PATH"
fi

# Rendered paths are literal: dollar signs and backticks must never expand.
# shellcheck disable=SC2016
MAGI_REAL_BIN='/Users/choegeun-won/.local/share/uv/tools/magi/bin/magi'
# shellcheck disable=SC2016
REVIEW_GATE='/Users/choegeun-won/.local/share/review-gate/review_gate.py'
# shellcheck disable=SC2016
PYTHON='/usr/bin/python3'

subcommand=
subcommand_found=
help_requested=
for arg do
    case "$arg" in
        --help|-h|--version|--schema|--examples|--examples=*) help_requested=yes ;;
    esac
    if [ -z "$subcommand_found" ]; then
        case "$arg" in
            -*) ;;
            *) subcommand=$arg; subcommand_found=yes ;;
        esac
    fi
done

if [ -n "$help_requested" ] || [ -z "$subcommand_found" ]; then
    exec "$MAGI_REAL_BIN" "$@"
fi
case "$subcommand" in
    lanes|doctor|report|report-plan|plan|publish|sample|quality) exec "$MAGI_REAL_BIN" "$@" ;;
esac

log_event() {
    "$PYTHON" "$REVIEW_GATE" log-event --kind "$1" --detail "$2"
}

gate_available() {
    [ -x "$PYTHON" ] && [ -f "$REVIEW_GATE" ] && [ -r "$REVIEW_GATE" ]
}

ticket_rejected=
if [ -n "${MAGI_REVIEW_TICKET-}" ] && gate_available; then
    if "$PYTHON" "$REVIEW_GATE" check-ticket --consume "$MAGI_REVIEW_TICKET"; then
        exec "$MAGI_REAL_BIN" "$@"
    else
        ticket_status=$?
        # Policy refusals are already logged atomically by the consuming check.
        if [ "$ticket_status" -eq 2 ]; then ticket_rejected=yes; fi
    fi
    printf '%s\n' 'magi: review-gate rejected the ticket or could not run; ticketed review refused.' >&2
fi

if [ "${#MAGI_GATE_BYPASS}" -ge 8 ]; then
    if ! log_event bypass "subcommand=$subcommand reason=$MAGI_GATE_BYPASS"; then
        printf '%s\n' 'magi: warning — bypass audit logging failed' >&2
    fi
    exec "$MAGI_REAL_BIN" "$@"
fi

# Resolve symlinks and '..' before testing containment; a textual prefix is insufficient.
if "$PYTHON" -c '
import os
import sys
home = os.environ.get("HOME")
if not home:
    sys.exit(1)
root = os.path.realpath(os.path.join(home, ".fido-automation"))
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--repo-dir" and i + 1 < len(args):
        value = args[i + 1]
    elif arg.startswith("--repo-dir="):
        value = arg.split("=", 1)[1]
    else:
        continue
    if value:
        path = os.path.realpath(value)
        if path != root and os.path.commonpath([root, path]) == root:
            sys.exit(0)
sys.exit(1)
' "$@" 2>/dev/null; then
    if log_event legacy-exempt "subcommand=$subcommand argv=$*"; then
        exec "$MAGI_REAL_BIN" "$@"
    fi
    printf '%s\n' 'magi: legacy exemption audit logging failed; review refused.' >&2
fi

if [ -n "${MAGI_REVIEW_TICKET-}" ] && ! gate_available; then
    printf '%s\n' 'magi: review-gate missing or unrunnable; ticketed review refused (fail-closed).' >&2
fi
if [ -z "$ticket_rejected" ]; then
    log_event rejected "subcommand=$subcommand argv=$*" 2>/dev/null || :
fi
printf '%s\n' 'magi: review refused — no review-gate ticket. Start one: review_gate.py start --item <ID> --reviewer magi --commit <SHA>, then run via review_gate.py run --ticket <T> -- magi ...  (magi development: set MAGI_GATE_BYPASS="<reason>")' >&2
exit 2
