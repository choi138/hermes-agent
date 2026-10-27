#!/usr/bin/python3
"""hermes-delegate: Mac entry point for koharu Codex delegations, Claude runs and device QA.

  codex   validate + dry-run the runner, then print the bare argv the gateway owns
  review  issue a review-gate ticket and launch a gated read-only Claude or Codex review
          (Claude: cmux by default, --via direct; Codex: direct by default, --via cmux)
  claude  launch a write-capable Claude Code implementation run (cmux by default)
  qa      launch a device/simulator QA run (Codex gpt-6-astra/high, fixed) in cmux
  smoke   tiny auth/exec check of the codex or claude CLI (not a review, no ticket)
  wait    block until a run dir has its rc file (for a background watcher)
  status  instant, non-blocking state of a run dir or codex output dir (check once, never poll)
  cancel  stop a launched run: SIGTERM its inner.zsh, which stops the worker and records the rc
"""

import sys
import hermes_delegate_codex as codex
import hermes_delegate_common as common
import hermes_delegate_review as review
import hermes_delegate_smoke as smoke
import hermes_delegate_spec as spec
import hermes_delegate_status as status
import hermes_delegate_watch as watch
import hermes_delegate_worker as worker


USAGE = """usage: hermes-delegate <command> [options]
  spec-check --spec F [--workdir D] [--kind codex|claude|qa]
          Read-only SPEC validation; workdir defaults to F's directory, kind to codex.
          Prints ok (exit 0), or each problem on stderr (exit 2).
  spec-check --template
          Print a fill-in SPEC skeleton (exit 0).

  codex   --spec F --workdir D --class C --timeout SEC [--risk R] [--ambiguity A] [--phase P]
          [--implementation-class I] [--contract-resolved] [--choice-reason T]
          [--deeper-analysis-evidence T] [--hard-judgment] [--high-failure-cost]
          [--failure-kind K --prior-model M --prior-effort E --attempt N]
          [--prior-action T] [--correction-evidence T] [--handoff-ref T]... [--max-attempts N]
          [--missing-context | --broken-environment | --deterministic-tool-sufficient]
          [--allowed-root D] [--output-dir D] [--sandbox workspace-write] [--code-scope P...]
          [--report REL]
          --report names an implementer report inside the workdir, verbatim in SPEC.
          Required for mechanical/bounded, including classes the runner remaps to them;
          needs --sandbox workspace-write.
          With --report, a worker exiting 0 without a new, non-empty, regular report
          under the workdir makes the runner exit 76 with acceptance.status=failed.
          --workdir must be a git worktree root. --code-scope takes files or directories
          relative to --workdir (directories expand to the runner-accepted files).
          --task-class is accepted as an alias of --class. Omit --sandbox for read-only.
          frontier (xhigh) needs --deeper-analysis-evidence with a real reference.
          Dry-runs the runner, checks its registration baseline, and prints ONE bare argv
          line for terminal(background=true, notify_on_complete=true). Never runs the worker.
          Rejects --model/--effort/--tier and every other override or --progress-* option.
          Exit: 0 line printed; 3 policy skipped (no worker needed, nothing printed);
          74 runner policy blocked; 2 usage; 1 other failure.
  review  --reviewer claude|codex --item ID --commit SHA --prompt F --workdir D [--base SHA]
          [--label L] [--timeout SEC] [--via direct|cmux] [--owner-approval ID]
          [--check | --no-launch]
          The item must already be opened with review-gate open. The workdir must be a clean
          checkout of SHA. Runs review-gate start, writes a run dir (diff.patch = git diff
          --binary BASE SHA with --base, else git show SHA; log.txt = git log/show --stat) and
          runs review-gate run -- <reviewer> with prompt.md = F + the standard review rules
          (P0/P1 only, file:line + concrete trigger, round-2 scope, exact approval line
          "APPROVED: no P0/P1").
          claude: claude -p --model opus --effort high, Read/Glob/Grep only, stream-json;
            --via cmux (default) or direct (OAuth token file, no cmux).
          codex: codex exec --ephemeral -s read-only -m gpt-6.1-sol, effort high (fixed);
            --via direct (default) or cmux.
          --check is a read-only preflight (no ticket, no auth check: use smoke). --no-launch
          issues a REAL ticket and skips the launch; that ticket stays open until launched.
          The verdict is never recorded automatically: koharu reproduces the findings, then
          runs review-gate close (PASS: --blockers 0 or omitted; BLOCK: --blockers N>=1).
          A pending verdict refuses a new round: koharu reproduces it, runs review-gate
          close itself and retries (it tells the user only if it cannot reproduce it).
          Round 3+ needs the user's approval: koharu asks in Discord and passes that
          message ID with --owner-approval ID (15-22 digits, passed to review-gate start;
          an ID already used on this item is refused). The user only gives that approval.
  review  --launch-run RUN_DIR
          Launches an already prepared run dir whose ticket is still open.
  claude  --spec F --workdir D --class {claude_choices} --timeout SEC
          [--advisor opus] [--label L] [--via cmux|direct] [--no-launch]
          Claude Code implementation run (claude -p, write-capable: acceptEdits inside the
          worktree, Bash allowed, no MCP, stream-json). --via cmux (default) runs in a new
          cmux workspace with the Keychain login. --class picks model/effort:
          {claude_classes}.
          {claude_direct}
          --advisor opus is opt-in per run (per-run settings override, never settings.json);
          refused for classes that already run on Opus.
          --workdir must be a clean git worktree root. Rejects --model/--effort and any
          permission/tool override. No review-gate: this is implementation, not review.
  qa      --spec F --workdir D --timeout SEC [--label L] [--no-launch]
          Device/simulator QA run: codex exec -m {qa[model]}, effort {qa[effort]}, sandbox
          {qa[sandbox]} (fixed user pin; no model/effort/sandbox flags accepted) in a
          new cmux workspace (GUI Keychain for private-registry tokens and code signing).
          --workdir must be a clean git worktree root.
  claude|qa --launch-run RUN_DIR
          Launches a prepared run dir (after --no-launch or a failed launch).
          review/claude/qa print run_dir=, rc=, via= and a watcher: line to run verbatim.
          A run dir launches once: inner.zsh takes RUN_DIR/lock, --launch-run refuses it.
  smoke   codex|claude [--via direct|cmux] [--timeout SEC]
          Tiny auth/exec check (codex: gpt-6.1-sol low; claude: haiku), prints ok or the
          reason; exit 0 ok, 1 failed. Not a review, no ticket. Default via: codex direct,
          claude cmux.
  wait    RUN_DIR [--timeout SEC] [--start-grace SEC] [--stall-minutes M]
          Blocks until RUN_DIR/<kind>.rc exists (kind from meta.json: review, claude or
          qa); prints status=started|finished|never_started|no_rc|stalled and exits with
          the worker rc, 90 when the run never started, 91 (status=no_rc) when no rc
          appeared: either --timeout passed or wait itself was signalled. In both cases
          the run may still be going: re-arm by running the printed watcher: line.
          92 (status=stalled) when <kind>.out has not grown for M minutes (default
          claude 20, review 20, qa 30; 0 disables), counted from the later of its last
          growth and this wait's start, so a re-run wait never fires at once. It prints
          the last growth time, the minutes stalled and the last tool call/event
          (tokens masked), stall_count=N (RUN_DIR/stall_count), a ready watcher: line
          (remaining --timeout, same --stall-minutes) and a watcher_no_stall: line
          (--stall-minutes 0), and never stops the run: re-arm with one of them or stop
          it with cancel. From the 2nd stall of a run only watcher_no_stall: is printed
          and a wait without --stall-minutes no longer checks for stalls (each re-arm
          spends one gateway continuation). An rc always wins over a stall.
          Writes RUN_DIR/review.result.md or claude.result.md; qa reports qa.final.md.
          Masks any sk-ant-oat token found in run dir outputs and warns. After rc 0 of a
          cmux run it closes that run's cmux workspace (failed runs keep theirs).
          A SIGTERM/SIGHUP/SIGINT to wait (watcher cancel, gateway stop/restart, ssh drop)
          only ends wait (status=no_rc, exit 91, "wait stopped by SIG...; the run keeps
          going"); the run keeps going, cmux or direct. Re-watch with wait; stop the run
          only with hermes-delegate cancel.
  status  PATH
          Instant, non-blocking check (no sleep, no wait) of PATH: a run dir under
          STATE/review|claude|qa/<name> or a codex output dir STATE/codex/<ts>-<rand>
          (a trailing slash is accepted). After launched=yes or the printed codex argv,
          check at most once with hermes-delegate status PATH; never sleep+peek.
          Prints key=value lines: kind=review|claude|qa|codex,
          status=not_started|running|finished|unknown, then where known rc=, pid= (live
          inner.zsh or codex runner), started_at=, ended_at=, last_output_age_s= (since
          <kind>.out or events.jsonl last changed), stall_count=, and for codex task_dir=,
          events=, runner_status= (from status.json); the last line is hint=.
          Exit 0 not_started/running/finished, 3 unknown (no rc and no live process,
          malformed rc/status.json, meta kind mismatch), 2 usage or a path outside the
          state root.
  cancel  RUN_DIR [--grace SEC]
          Sends SIGTERM to this run's inner.zsh (RUN_DIR/inner.pid, checked against the
          live process), which stops the worker group or review-gate run and records the
          real rc. A run whose inner.zsh has not started yet is fenced (lock taken, rc 143).
          Prints status=finished|no_rc; exits with the rc, 91 when none within --grace
          (default 30). The only way to stop a run. A cancelled review ticket still needs
          review-gate close.
""".format(claude_classes=", ".join("{}={}/{}".format(k, v["model"], v["effort"])
                                    for k, v in common.CLAUDE_IMPL_POLICY.items()),
           claude_choices="|".join(common.CLAUDE_IMPL_POLICY), qa=common.QA_POLICY,
           claude_direct=("--via direct runs detached without cmux, with the OAuth token file\n"
                          "          (0600) exported only to the claude process." if common.CLAUDE_IMPL_DIRECT else
                          "--via direct is refused here: the token would reach Bash children\n"
                          "          (direct is for claude reviews only)."))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0 if argv else 2
    commands = {"codex": codex.cmd_codex, "review": review.cmd_review, "wait": watch.cmd_wait, "smoke": smoke.cmd_smoke,
                "cancel": watch.cmd_cancel, "status": status.cmd_status, "spec-check": spec.cmd_spec_check,
                "claude": lambda rest: worker.cmd_worker("claude", rest),
                "qa": lambda rest: worker.cmd_worker("qa", rest)}
    if argv[0] not in commands:
        print(f"hermes-delegate: unknown command {argv[0]!r}; see --help", file=sys.stderr)
        return 2
    try:
        return commands[argv[0]](argv[1:])
    except common.Fail as exc:
        print("hermes-delegate: " + str(exc).replace("\n", " "), file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"hermes-delegate: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
