"""Conversational progress decisions. No I/O or worker control."""
import hashlib
import json
import re


def safe_label(value):
    # Reject entire labels, never repair an injection into plausible authority.
    if (not isinstance(value, str) or not re.fullmatch(r'[가-힣A-Za-z0-9 -]{1,64}', value)
            or re.search(r'MEDIA|https?|www|(?:sk|ghp|github)[-_]|[A-Za-z0-9]{24,}', value, re.I)
            or not value.strip()):
        return '작업'
    return value.strip()


def fingerprint(observation):
    if not observation['available']:
        return None
    # Index/HEAD changes alone are not code changes. Deleted files contribute None.
    return hashlib.sha256(json.dumps({p: f['content'] for p, f in observation['files'].items()},
                                    sort_keys=True).encode()).hexdigest()


def stage(snapshot):
    status = snapshot.get('exit_status', 'running')
    coordinator = snapshot.get('coordinator_stage', 'working')
    if coordinator == 'stopped':
        return 'stopped', '중단'
    if status in ('cancelled', 'timed_out'):
        return status, '실행 중단'
    if status not in ('running', 'needs_user', 'cli_completed'):
        return status, '실행 실패'
    if snapshot.get('validation', {}).get('applicable'):
        return 'final_verified', '최종 검증 완료'
    if status == 'needs_user':
        return 'needs_user', '사용자 확인 대기'
    if coordinator in ('verifying', 'final_verified'):
        return 'verifying', '레나 검증 중'
    if status == 'cli_completed':
        return 'cli_completed', 'CLI 종료'
    return 'observing', '관측 중'


def render(snapshot, *, limit=900):
    key, title = stage(snapshot)
    worker = 'Claude' if snapshot.get('worker_cli') == 'claude' else 'Codex'
    changes = snapshot.get('display_changes', snapshot.get('changes', []))
    content = [c for c in changes if set(c['kinds']) & {'content', 'added', 'deleted', 'reverted'}]
    if content:
        counts = {label: sum(c['class'] == kind for c in content) for kind, label in
                  [('source', '소스'), ('tests', '테스트'), ('docs', '문서'), ('build', '빌드 설정')]}
        change = ', '.join(f'{label} {count}개' for label, count in counts.items() if count) + ' 파일 내용 변경 관측'
    elif changes:
        change = '스테이징·커밋 등 git 상태만 변경 관측'
    else:
        change = '새로 확인된 파일 변경 없음'
    if not snapshot.get('files_available', snapshot.get('available', True)):
        change = (change + ', 파일 관측 일부 미수집') if changes else '파일 관측 일부 미수집'
    tests = snapshot.get('tests', {})
    validation = snapshot.get('validation', {})
    if validation.get('applicable'):
        verify = f"레나 검증 {validation['passed']}개 통과를 확인했어."
        if validation.get('skipped'):
            verify += f" {validation['skipped']}개는 건너뛰었어."
    elif tests.get('status') == 'passed':
        verify = f"{worker} 테스트 {tests.get('passed', 0)}개 통과"
        verify += '가 현재 코드에서 확인됐어.' if tests.get('applicable') else ' 기록은 있지만, 현재 코드 적용 미확인 상태야.'
    else:
        verify = {'failed': f'{worker} 테스트 실패 기록이 있어. 최종 검증은 아직이야.',
                  'in_progress': f'{worker} 테스트가 시작됐어. 결과 미수집 상태야.'}.get(tests.get('status'), '')
    action = {
        'observing': f'{worker}에 맡긴 작업의 진행 근거를 확인하고 있어. 실행 결과와 레나 검증이 남아 있어.',
        'cli_completed': f'{worker} CLI 종료를 확인했어. 레나 검증은 아직 시작 전이야.',
        'verifying': '레나가 결과를 검증하고 있어. 검증 결과가 나오면 알려줄게.',
        'needs_user': '사용자 확인 대기 중이야. 응답을 받은 뒤 이어갈 수 있어.',
        'final_verified': '최종 검증 완료. 승인받은 범위의 확인을 마쳤어.',
        'stopped': '작업이 중단됐어. 중단 사유를 확인하고 다음 조치를 정해야 해.',
    }.get(key, '실행 실패·중단이 확인됐어. 원인을 확인하고 다음 조치를 정해야 해.')
    if snapshot.get('monitoring') == 'stale':
        action += ' 20분 동안 새 진행 근거가 없어. 작업 상태를 확인해야 해.'
    elif snapshot.get('monitoring') == 'lost':
        action += ' 관측 채널을 읽지 못하고 있어. 작업 실패인지는 아직 알 수 없어.'
    details = [change + '.', verify] if changes or not snapshot.get('files_available', snapshot.get('available', True)) else [verify]
    text = f"{safe_label(snapshot.get('task_label'))}: {action}"
    detail = ' '.join(part for part in details if part)
    if detail:
        text += '\n' + detail
    if len(text) > min(limit, 900):
        raise ValueError('report_limit')
    return text


def plan(state, snapshot, now, interval):
    """Return ordered (operation, event identity, event label, content) items."""
    key, _ = stage(snapshot)
    signature = [snapshot.get('code_fingerprint'), snapshot.get('execution'),
                 snapshot.get('tests'), snapshot.get('exit_status'), snapshot.get('validation'),
                 snapshot.get('coordinator_stage')]
    fresh = signature != state.get('evidence_signature') or bool(snapshot.get('changes'))
    if fresh:
        state['last_evidence_at'] = now
        state['evidence_signature'] = signature
    # A bounded file inventory is not a lost channel. Event/manifest read loss is.
    errors = snapshot['current_errors']
    lost = (('git_unavailable' in errors and 'git_truncated' not in errors) or
            any(e in errors for e in ('events_unavailable', 'events_replaced_or_truncated',
                                     'events_truncated', 'manifest_unavailable')))
    monitoring = 'lost' if lost else 'stale' if now - state.get('last_evidence_at', now) >= 1200 else None
    if not lost and state.get('monitoring') == 'lost':
        state['last_evidence_at'] = now
        monitoring = None
    snapshot['monitoring'] = monitoring
    result = []
    identities = []
    terminal = key not in ('observing', 'verifying', 'cli_completed', 'needs_user')
    if terminal:
        monitoring = snapshot['monitoring'] = None
    status_known = 'manifest_unavailable' not in snapshot['current_errors']
    state.setdefault('waiting', 'needs_user' in state.get('terminal_seen', []))
    if terminal:
        identities.append((key, key))
    elif key == 'needs_user':
        if not state.get('waiting') and status_known:
            state['wait_episode'] = state.get('wait_episode', 0) + 1
            state['waiting'] = True
            identities.append((f"needs_user:{state['wait_episode']}", key))
    elif status_known and not any(error.startswith('events_') for error in snapshot['current_errors']):
        # File inventory gaps do not hide a known resume. An incomplete event
        # channel cannot establish that the preceding wait episode has ended.
        state['waiting'] = False
    if monitoring and monitoring != state.get('monitoring') and not terminal:
        state['monitor_episode'] = state.get('monitor_episode', 0) + 1
        identities.append((f"monitor:{state['monitor_episode']}", monitoring))
    state['monitoring'] = monitoring
    # A worker exit is not coordinator verification. Both transitions are prompt;
    # adjacent transitions share one notice while the card shows the latest phase.
    phase_changed = key != state.get('last_stage')
    if key in ('cli_completed', 'verifying') and phase_changed:
        if key == 'cli_completed' or now - state.get('review_transition_at', -1e30) > 30:
            identities.append((key, key))
        state['review_transition_at'] = now
    state['last_stage'] = key
    text = render(snapshot)
    events = state.setdefault('reported_events', [])
    identities = [(f"{snapshot['run_id']}:{identity}", event) for identity, event in identities]
    immediate = (phase_changed and key != 'observing') or any(identity not in events for identity, _ in identities)
    card_due = now - state.get('last_card_at', -1e30) >= 30
    if (state.get('card_content') != text and (card_due or immediate)
            and not state['pending']):
        operation = 'CARD_PATCH' if state.get('card_created') else 'CARD_CREATE'
        result.append((operation, f"{snapshot['run_id']}:card:{state['sequence'] + 1}", key, text))
        state['card_created'] = True
        state['card_content'] = text
        state['last_card_at'] = now
    # Terminal card MUST precede terminal notice, even when an old write is pending.
    if terminal and state.get('card_content') != text:
        return result
    for identity, event in identities:
        if identity not in events:
            result.append(('NOTICE', identity, event, text))
            events.append(identity)
            state['last_notice_at'] = now
    state.setdefault('last_notice_at', now)
    if terminal:
        state['closing'] = True
        state['final_snapshot'] = snapshot
    return result
