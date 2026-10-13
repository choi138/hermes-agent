"""Gateway-owned factual outbox backed by the selected V3 evidence collector.

No V3 card edits or sender run here; the source gateway owns delivery receipts.
"""
import argparse
import json
import math
import re
from pathlib import Path
from agent.delegation_progress_policy import stage as evidence_stage
from agent.delegation_progress import (Manifest, Progress as V3Progress, TERMINAL,
    _atomic, _read, _lock, _private_dir, file_class)

def safe_source_name(path):
    """Conservative relative source names only; never reinterpret media or links.

    Reject rather than repair attacker-controlled names. ASCII punctuation is
    allowlisted and long/high-entropy credential-like segments are suppressed.
    Labels and all file contents remain local. Underscores are escaped for Discord.
    """
    if (not isinstance(path, str) or not 1 <= len(path) <= 110
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or path.startswith(('/', '.')) or any(p in ('', '.', '..') for p in path.split('/'))
            or not file_class(path) or re.search(r"MEDIA|https?|www\.|[A-Za-z0-9]{24,}|(?:sk|ghp|github_pat|xox[baprs])[-_]", path, re.I)):
        return "안전한 이름 표시 불가"
    return path.replace('_', r'\_')

def render(snapshot, *, limit=1200):
    """Fixed factual operations plus bounded, escaped source names; no raw text."""
    if not 200 <= limit <= 1900:
        raise ValueError("message_limit")
    lines = ["작업 진행 상황을 전해드려요."]
    if snapshot.get("baseline"):
        lines.append("• 기준 상태를 기록했어요. 기존 수정은 이번 구간의 변경으로 세지 않았어요.")
    if not snapshot.get("available"):
        lines.append("• 상태 확인 불가: 이번 보고 구간에 일부 관측을 읽지 못했거나 수집 한도에 걸렸어요. 변경 없음으로 판단하지 않았어요.")
    changes = snapshot.get("changes", [])
    if changes:
        labels = {"source": "소스", "tests": "테스트", "docs": "문서", "build": "빌드 설정"}
        counts = {key: sum(c["class"] == key for c in changes) for key in labels}
        summary = ", ".join(f"{labels[key]} {count}개" for key, count in counts.items() if count)
        lines.append(f"• 새로 확인: {summary} 파일의 내용·git 상태가 달라졌어요.")
        operations = {"added": "파일 추가", "content": "파일 내용 변경", "staged": "스테이징",
                      "reverted": "수정 되돌림", "deleted": "삭제", "committed": "커밋 반영", "status": "git 상태 변경"}
        for change in changes[:5]:
            observed = ["테스트 파일 추가" if kind == "added" and change['class'] == "tests" else operations[kind]
                        for kind in change['kinds'] if kind in operations]
            lines.append(f"• {safe_source_name(change['path'])}: {', '.join(observed)}.")
        if len(changes) > 5:
            lines.append(f"• 이름 목록에서 {len(changes) - 5}개 파일을 생략했어요.")
        kinds = {kind for c in changes for kind in c["kinds"]}
        special = [label for kind, label in (("staged", "스테이징"), ("committed", "커밋 반영"),
                   ("reverted", "수정 되돌림"), ("deleted", "삭제")) if kind in kinds]
        if special:
            lines.append("• 확인된 상태 변화: " + ", ".join(special) + ".")
    elif snapshot.get("available") and not snapshot.get("baseline"):
        lines.append("• 새로 확인된 파일 변경 없음. 관측 사이의 작업까지 없었다고 단정하지는 않아요.")
    phase = snapshot.get("execution", {}).get("phase", "unknown")
    evidence = {"execution_started": "도구 실행 시작 기록이 있어요. 완료 여부는 확인 전이에요.",
                "execution_completed": "도구 실행 완료 기록이 있어요. 기능 완성을 뜻하지는 않아요.",
                "file_change_completed": "파일 변경 도구 완료 기록이 있어요. 실제 변경 범위는 git 관측 기준이에요."}
    if snapshot.get("execution_since_queue") and phase in evidence:
        lines.append("• 실행 근거: " + evidence[phase])
    elif not changes and snapshot.get("liveness") == "alive":
        lines.append("• 프로세스는 확인됐지만, 새 구현 활동 근거가 부족해요.")
    tests = snapshot.get("tests", {"status": "unknown"})
    test_text = {"unknown": "결과 확인 전이에요.",
                 "in_progress": "시작 기록이 있어요. 완료 근거는 아직 없어요.",
                 "failed": "마지막 pytest 실행 실패가 확인됐어요.",
                 "passed": "마지막 개별 pytest 실행 통과가 확인됐어요. 현재 변경 전체의 재검증 여부는 미확인이에요."}
    lines.append("• 테스트: " + test_text.get(tests["status"], test_text["unknown"]))
    if tests.get("scope") == "canonical_command" and tests["status"] == "passed":
        lines.append(f"• 최근 표준 테스트 명령: {tests['passed']}개 통과, {tests.get('skipped', 0)}개 건너뜀. 이후 수정의 승인 근거는 아니에요.")
    elif tests.get("skipped"):
        lines.append(f"• 최근 개별 테스트에서 {tests['skipped']}개를 건너뛰었어요.")
    stage, status = snapshot.get("coordinator_stage"), snapshot.get("exit_status", "running")
    if stage == "final_verified":
        lines.append("• 최종 검증 완료. 승인된 작업 범위의 진행 보고를 마칠게요.")
        lines.append("• 운영 활성화 여부는 이 보고로 확인하지 않아요.")
    elif stage == "stopped":
        lines.append("• 명시적 중단 요청으로 진행 보고를 마칠게요. 작업 완료 판정은 아니에요.")
    else:
        if status == "cli_completed":
            lines.append("• 위임 실행은 종료됐어요." if stage == "verifying" else "• 위임 실행 종료, 결과 검증 대기.")
        elif status in TERMINAL:
            lines.append("• 작업자 정리에 실패했어요. 프로세스가 남아 있을 수 있어 확인이 필요해요." if status == "cleanup_failed" else "• 위임 실행 실패·중단이 확인됐어요. 후속 판단을 기다려요.")
        elif status == "needs_user":
            lines.append("• 사용자 확인 대기 중이에요.")
        if stage == "verifying":
            lines.append("• 결과 검증 진행 중: 조정자 상태에 명시됐어요. 결과는 아직 미확인이에요.")
        lines.append("• 아직 미검증: 결과 검증.")
    text = "\n".join(lines)
    if len(text) > limit:
        suffix = "\n• 길이 제한으로 일부 설명을 생략했어요."
        text = text[:limit - len(suffix)].rstrip('\\') + suffix
    return text

class GatewayProgress(V3Progress):
    def __init__(self, manifest, state_dir, **kwargs):
        if manifest.delivery_owner != "gateway":
            raise ValueError("gateway_owner_required")
        kwargs.setdefault("interval", 300)
        super().__init__(manifest, state_dir, **kwargs)

    def _tick(self, state, now):
        if state["stopped"]:
            return {"snapshot": state["final_snapshot"], "queued": [], "stopped": True}
        snapshot = self._observe(state, now)
        snapshot["changes"] = list(state["accumulated"].values())
        snapshot["available"] = snapshot["available"] and not snapshot["errors"]
        queued = []
        terminal = snapshot["exit_status"] if snapshot["exit_status"] != "running" else None
        stage = evidence_stage(snapshot)[0]
        if stage in ("final_verified", "verifying", "stopped"):
            snapshot["coordinator_stage"] = stage
        if stage in ("final_verified", "stopped") and not self.manifest_error:
            terminal = stage
        if stage == "verifying" and "verifying" not in state["terminal_seen"]:
            terminal = "verifying"
        # Migrate old states conservatively: a historical wait stays the same
        # episode until a complete running observation proves resumed work.
        state.setdefault("waiting", "needs_user" in state["terminal_seen"])
        if snapshot["exit_status"] == "running" and not snapshot["current_errors"]:
            state["waiting"] = False
        immediate = (not state["waiting"] and not self.manifest_error if terminal == "needs_user"
                     else terminal is not None and terminal not in state["terminal_seen"])
        if state["last_queued_at"] is None:
            state["last_queued_at"] = now
            state["last_queued_tests"] = dict(state["tests"])
            state["last_queued_errors"] = list(state.get("observation_errors", []))
        meaningful = bool(state["accumulated"] or state["execution_since_queue"] or state["tests"] != state.get("last_queued_tests", {"status": "unknown"}) or (state["observation_errors"] and state["observation_errors"] != state.get("last_queued_errors", [])))
        due = terminal is None and meaningful and now - state["last_queued_at"] >= self.interval and not state["pending"]
        if immediate:
            state["pending"] = [message for message in state["pending"] if message.get("admitted")]
        if immediate or due:
            if len(state["pending"]) >= 32:
                raise ValueError("outbox_full")
            state["sequence"] += 1
            message = {"id": f"{self.manifest.run_id}:{state['sequence']}",
                       "run_id": self.manifest.run_id, "sequence": state["sequence"],
                       "thread_id": self.manifest.thread_id, "observed_at": now,
                       "event": terminal if immediate else "periodic",
                       "content": render(snapshot), "allowed_mentions": {"parse": [], "replied_user": False}}
            message.update(self.manifest.sender_identity())
            state["pending"].append(message)
            state["last_queued_tests"] = dict(state["tests"])
            state["last_queued_errors"] = list(state["observation_errors"])
            state["accumulated"] = {}
            state["execution_since_queue"] = False
            state["observation_errors"] = []
            state["last_queued_at"] = now
            if immediate:
                if terminal == "needs_user":
                    state["waiting"] = True
                else:
                    state["terminal_seen"].append(terminal)
            queued.append(message)
        if terminal in ("final_verified", "stopped"):
            state["stopped"] = True
            state["final_snapshot"] = snapshot
        return {"snapshot": snapshot, "queued": queued, "stopped": state["stopped"]}

    def tick(self, *, now=None, dry_run=False):
        now = self.clock() if now is None else now
        if isinstance(now, bool) or not math.isfinite(now):
            raise ValueError("observation_clock")
        if dry_run:
            return self._tick(self._load(), now)
        _private_dir(self.root)
        _private_dir(self.directory)
        with _lock(self.directory / "delivery.lock", wait=True), self._transaction():
            with self._tick_fence():
                state = self._load()
                result = self._tick(state, now)
                _atomic(self.path, state)
                return result

def observe(manifest_path, state_dir, thread, *, ack=None, admit=None, stop=False):
    path = Path(manifest_path)
    manifest = Manifest.load(path)
    if manifest.delivery_owner != 'gateway':
        raise ValueError('gateway_owner_required')
    if manifest.thread_id != thread:
        raise ValueError('gateway_thread_binding')
    progress = GatewayProgress(manifest, state_dir)
    if ack:
        progress.ack(ack)
    if admit:
        message = progress.admit(admit)
        return {'status': 'admitted', 'run_id': manifest.run_id, 'message': message}
    if stop:
        with _lock(progress.directory / "delivery.lock", wait=True), progress._transaction():
            state = progress._load()
            state['pending'] = [message for message in state['pending'] if message.get('admitted')]
            state['stopped'] = True
            state['final_snapshot'] = progress._observe(state, progress.clock())
            _atomic(progress.path, state)
        return {'status': 'stopped', 'run_id': manifest.run_id}
    tick = progress.tick()
    # Start is evidence of a spawned worker, not a shell launch acknowledgement.
    try:
        started = json.loads(_read(path.parent, path.name + '.worker-start.json', 4096))
    except FileNotFoundError:
        started = None
    if started is not None:
        if (started.get('run_id') != manifest.run_id or type(started.get('pid')) is not int
                or started['pid'] <= 0 or started.get('cli') not in {'codex', 'claude'}):
            raise ValueError('worker_incarnation')
        with _lock(progress.directory / 'delivery.lock', wait=True), progress._transaction():
            state = progress._load()
            if state.get('start_observed') and state['start_observed'] != started:
                raise ValueError('worker_incarnation_changed')
            if not state.get('start_observed'):
                state['start_observed'] = started
                if not state['terminal_seen'] and not state['stopped']:
                    state['sequence'] += 1
                    state['pending'].insert(1 if state['pending'] and state['pending'][0].get('admitted') else 0, {
                        'id': f"{manifest.run_id}:{state['sequence']}",
                        'run_id': manifest.run_id, 'sequence': state['sequence'],
                        'thread_id': thread, 'event': 'started',
                        'content': f"🔀 {started['cli'].capitalize()} 작업자 실행을 확인했어요. 진행 상황과 결과를 관찰하고 있어요.",
                        'observed_at': progress.clock(),
                        'allowed_mentions': {'parse': [], 'replied_user': False},
                        **manifest.sender_identity()})
                _atomic(progress.path, state)
    return {'status': 'observed', 'run_id': manifest.run_id,
            'message': progress.peek(), 'exit_status': tick['snapshot']['exit_status'],
            'worker': started}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--thread', required=True)
    parser.add_argument('--ack')
    parser.add_argument('--admit')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args(argv)
    try:
        value = observe(args.manifest, args.state_dir, args.thread, ack=args.ack, admit=args.admit, stop=args.stop)
        print(json.dumps(value, ensure_ascii=True))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else ''
        allowed = {'gateway_owner_required', 'gateway_thread_binding',
                   'worker_incarnation', 'worker_incarnation_changed',
                   'ack_requires_exact_head_id', 'admit_requires_exact_head_id',
                   'state_identity_or_schema_mismatch', 'invalid_state', 'outbox_full'}
        print(json.dumps({'status': 'observation_unavailable',
                          'error': reason if reason in allowed else 'observation_unavailable'}))
        return 74
