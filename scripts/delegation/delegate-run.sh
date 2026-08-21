#!/bin/bash
# delegate-run.sh — wrap a delegated (long-running) command and record its
# lifecycle to ~/.delegations/<label>/ so a Hermes cron monitor can observe it.
#
# This wrapper NEVER sends notifications. Reporting is the agent's job; this
# only leaves durable, machine-readable state behind.
#
# Usage:
#   delegate-run.sh <label> -- <command> [args...]
#
# Example:
#   delegate-run.sh toki-auth -- zsh -lic 'codex exec -C /path --skip-git-repo-check < /tmp/p.txt'
#
# Layout produced:
#   ~/.delegations/<label>/log         combined stdout+stderr of the command
#   ~/.delegations/<label>/cmd         the argv, one element per line
#   ~/.delegations/<label>/state.json  {"label","status","exit_code","pid",...}
#
# status: running | done | failed   (stalled is derived by the collector)

set -u

usage() {
  echo "usage: delegate-run.sh <label> -- <command> [args...]" >&2
  exit 64
}

[ "$#" -ge 3 ] || usage

label="$1"
shift
[ "$1" = "--" ] || usage
shift
[ "$#" -ge 1 ] || usage

# Label is embedded in paths and in monitor script filenames; keep it strict.
case "$label" in
  *[!A-Za-z0-9._-]*) echo "delegate-run: label must match [A-Za-z0-9._-]+ (got: $label)" >&2; exit 64 ;;
  ""|.|..) echo "delegate-run: invalid label" >&2; exit 64 ;;
esac

root="$HOME/.delegations/$label"
mkdir -p "$root" || exit 70

log="$root/log"
state="$root/state.json"

# Record argv losslessly, one element per line (avoids JSON escaping hazards).
: > "$root/cmd"
for a in "$@"; do printf '%s\n' "$a" >> "$root/cmd"; done

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Atomic state write: temp file in the same dir, then rename.
write_state() {
  # $1=status  $2=exit_code (JSON scalar: number or null)
  tmp="$state.tmp.$$"
  cat > "$tmp" <<EOF
{"label":"$label","status":"$1","exit_code":$2,"pid":$$,"started_at":"$started_at","updated_at":"$(now)"}
EOF
  mv -f "$tmp" "$state"
}

started_at="$(now)"
write_state running null

# Truncate the log for this run so a restarted delegation is not confused
# with the previous attempt's output.
: > "$log"
printf '=== delegate-run %s started %s ===\n' "$label" "$started_at" >> "$log"

# Run the delegated command. stdin is closed so a prompt cannot hang forever.
"$@" >> "$log" 2>&1 < /dev/null
rc=$?

printf '=== delegate-run %s exited rc=%s at %s ===\n' "$label" "$rc" "$(now)" >> "$log"

if [ "$rc" -eq 0 ]; then
  write_state done "$rc"
else
  write_state failed "$rc"
fi

exit "$rc"
