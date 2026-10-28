"""Authenticated request scope and authoritative task publication.

Turn completion remains distinct from verified task completion. Tool entry records
attempted execution before its effects, including legacy and direct shell paths.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import logging
import re
from uuid import uuid4

from agent.task_lifecycle.parent import ParentLedger
from agent.task_lifecycle.registry import Registry

logger = logging.getLogger(__name__)

_current = ContextVar('gateway_parent_completion', default=None)
_EXECUTION_TOOLS = frozenset({
    'terminal', 'execute_code', 'write_file', 'patch', 'apply_patch', 'edit_file',
    'file_write', 'file_edit', 'delegate_task', 'browser_exec',
})
_CODING_REQUEST = re.compile(
    r'(?i)(?:\b(?:implement|refactor|debug|fix|patch|build|modify|edit)\b'
    r'|구현해|구현하|수정해|수정하|고쳐|고치|코딩해|리팩터링해|개발해'
    r'|테스트해|테스트하|진행해|작업해|작업하)')
_QUESTION = re.compile(r'(?i)^(?:what|why|how|which|where|when)\b|뭐(?:야|였|지)|무엇|어떤|왜|어떻게')



@dataclass(frozen=True)
class AuthenticatedRequest:
    text: str
    owner: str
    profile: str
    origin: str


def capture_request(event):
    """Capture before plugin rewrites; this snapshot itself grants no authority."""
    source = event.source
    platform = source.platform.value if source.platform else ''
    identity = ':'.join(str(v or '') for v in (
        platform, source.chat_id, source.thread_id, event.message_id))
    return source, AuthenticatedRequest(
        text=event.text, owner=str(source.user_id or ''),
        profile=str(source.profile or 'default'), origin=identity)


def admit_request(event, captured):
    """Called only after the gateway authorized this exact original source."""
    source, request = captured
    current = capture_request(event)[1]
    if (event.source is source and not event.internal
            and (request.owner, request.profile, request.origin) ==
            (current.owner, current.profile, current.origin)):
        event._completion_request = request


def snapshot_request(event):
    request = getattr(event, '_completion_request', None)
    return asdict(request) if isinstance(request, AuthenticatedRequest) else None


def restore_request(event, encoded):
    """Only the durable admitted envelope calls this; wire metadata cannot mint it."""
    if not encoded or event.internal:
        return
    request = AuthenticatedRequest(**encoded)
    if (request.owner, request.profile) != (
            str(event.source.user_id or ''), str(event.source.profile or 'default')):
        raise ValueError('Admitted request identity mismatch')
    event._completion_request = request


def coding_request(text):
    if not isinstance(text, str):
        return False
    return bool(_CODING_REQUEST.search(text) and not (_QUESTION.search(text) and not re.search(
        r"(?i)please|can you|could you|해줘|해주|하라|수정해|구현해|고쳐", text)))


class CompletionScope:
    def __init__(self, ctx, publication_barrier, *, registry=None):
        self.ctx = ctx
        self.barrier = publication_barrier
        self.registry = registry
        self.ledger = None
        self.parent = None
        self.executed = False
        self.orphaned = False
        self.direct_write = False
        self.deferred = False
        self.error = None
        self.mode = 'support'
        self.previously_accepted = False
        self.rendered_response = None
        self.followup = bool((getattr(ctx.event, "metadata", None) or {}).get("process_followup"))
        self._owned_registry = registry is None
        request = getattr(ctx.event, '_completion_request', None)
        self.request = request if isinstance(request, AuthenticatedRequest) else None
        source = ctx.source
        self.identity = dict(session_key=ctx.session_key or '',
            profile=str(source.profile or 'default'), owner=str(source.user_id or ''))

    def defer(self):
        if not self.deferred:
            self.barrier()
            self.deferred = True

    def open(self):
        if self.ctx.event is not None:
            self.ctx.event._completion_decision = None
        try:
            self.registry = self.registry or Registry()
            from gateway.task_completion_managed import remote_snapshot_reader
            self.ledger = ParentLedger(self.registry, remote_snapshot=remote_snapshot_reader(self.registry))
            self.parent = self.ledger.current(**self.identity)
            from gateway.task_completion_modes import effective_mode
            self.mode = effective_mode(self.registry, self.identity['profile'],
                getattr(self.ctx, 'user_config', None) or {}, self.parent)
            self.orphaned = bool(self.registry._conn.execute(
                'SELECT 1 FROM lifecycle_parent_executions WHERE session_key=? AND profile=? '
                'AND owner=? AND parent_id IS NULL LIMIT 1',
                tuple(self.identity[k] for k in ('session_key', 'profile', 'owner'))).fetchone())
            previous_accepted = bool(self.parent and self.ledger.decision(self.parent['parent_id'])['accepted'])
            self.previously_accepted = previous_accepted
            from gateway.task_completion_intake import update_parent
            self.parent = update_parent(self.ledger, self.identity, self.request, self.mode,
                previous_accepted=previous_accepted)
            if self.orphaned or (self.parent and not self.ledger.decision(self.parent['parent_id'])['accepted']):
                self.defer()
        except Exception as exc:
            logger.warning('Task completion scope unavailable', exc_info=True)
            # Unknown authority always withholds official status, before streaming.
            self.error = str(exc)
            self.defer()
        return self

    def record(self, name):
        self.executed = True
        self.direct_write |= name in {'write_file', 'patch', 'apply_patch', 'edit_file', 'file_write', 'file_edit'}
        self.defer()
        if self.error:
            raise RuntimeError('Task completion ledger unavailable: ' + self.error)
        new_direct_objective = (self.parent is None or (self.previously_accepted
            and self.request and self.request.origin != self.parent['origin']))
        if new_direct_objective and self.direct_write and self.request and self.request.owner:
            self.parent = self.ledger.begin(**self.identity, origin=self.request.origin,
                original_request=self.request.text)
            from gateway.task_completion_modes import remember_parent_mode
            remember_parent_mode(self.registry, self.parent['parent_id'], self.mode)
            self.previously_accepted = False
        if (self.direct_write and self.parent and self.request and self.request.owner
                and not self.ctx.event.internal and not getattr(self.ctx.source, 'is_bot', False)):
            # An actual tool effect is stronger evidence than a lexical intake
            # guess. Bind its authenticated source before invoking the handler.
            self.parent = self.ledger.append_requirement(self.parent['parent_id'],
                owner=self.request.owner, origin=self.request.origin, source_text=self.request.text)
        self.ledger.record_execution(execution_id=str(uuid4()), **self.identity,
            kind=name, parent_id=self.parent['parent_id'] if self.parent else None,
            source_request=asdict(self.request) if self.request else None)

    def decision(self):
        from gateway.task_completion_modes import effective_mode, publication_decision
        if self.error:
            return self._evidence_decision()
        if self.registry is not None:
            try:
                self.mode = effective_mode(self.registry, self.identity['profile'],
                    getattr(self.ctx, 'user_config', None) or {}, self.parent)
            except Exception as exc:
                logger.warning('Task completion policy unavailable', exc_info=True)
                self.error = str(exc)
                self.defer()
                return self._evidence_decision()
        return publication_decision(self._evidence_decision(), self.mode)

    def _evidence_decision(self):
        if self.error:
            return dict(accepted=False, status='unavailable', missing=[self.error], parent_id=None)
        if self.parent:
            from gateway.task_completion_requests import task_status
            if (self.previously_accepted and self.request and not self.executed
                    and not coding_request(self.request.text) and not task_status(self.request.text)):
                return None
            try:
                return self.ledger.decision(self.parent['parent_id'])
            except Exception as exc:
                logger.warning('Task completion decision unavailable', exc_info=True)
                return dict(accepted=False, status='unavailable', missing=[str(exc)],
                            parent_id=self.parent['parent_id'])
        if self.executed or self.orphaned:
            return dict(accepted=False, status='unverified', parent_id=None,
                        missing=['unattributed execution: no authenticated coding parent'])
        return None

    def render(self, response):
        if self.followup:
            return response
        decision = self.decision()
        if response == self.rendered_response and decision == getattr(self, 'rendered_decision', None):
            return response
        self.rendered_decision = decision
        if not decision:
            return response
        if self.mode == 'support' and decision['status'] != 'unavailable':
            from gateway.task_completion_delivery import execution_report
            self.rendered_response = execution_report(response)
            return self.rendered_response
        if decision['accepted']:
            self.rendered_response = ('[Hermes task: verified; parent ' + decision['parent_id']
                + '; decision ' + decision['decision_id'] + ']')
            return self.rendered_response
        # Preserve conversational replies but never expose an execution-bearing model
        # conclusion as the official completion result before acceptance.
        status = '[Hermes task: ' + decision['status'] + '; completion withheld]'
        missing = '\n'.join('- ' + value for value in decision['missing'])
        report = status + ('\n' + missing if missing else '')
        from gateway.task_completion_requests import task_status, adds_requirement
        if self.parent or self.error:
            # Pending task publication always comes from receipts. Explanatory
            # prose is quoted discussion, including after conservative shell audits.
            self.rendered_response = report
            if (self.request and not self.direct_write and _QUESTION.search(self.request.text)
                    and not adds_requirement(self.request.text, self.parent)
                    and not task_status(self.request.text)):
                discussion = '\n'.join('> ' + line for line in str(response or '').splitlines())
                self.rendered_response += '\n\n[Unverified discussion; task status is shown above]\n' + discussion
        elif self.direct_write or (self.request and (adds_requirement(self.request.text, self.parent)
                or task_status(self.request.text))):
            self.rendered_response = report
        else:
            self.rendered_response = str(response or '') + '\n\n' + report
        return self.rendered_response

    def close(self):
        if self._owned_registry and self.registry is not None:
            self.registry.close()


@contextmanager
def completion_scope(ctx, publication_barrier, *, registry=None):
    scope = CompletionScope(ctx, publication_barrier, registry=registry).open()
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
        scope.close()


def record_tool_execution(name):
    scope = _current.get()
    if scope is not None and name in _EXECUTION_TOOLS:
        scope.record(name)


def transform_output(response):
    scope = _current.get()
    return scope.render(response) if scope is not None else response


def finalize_result(scope, result):
    if not isinstance(result, dict):
        return result
    result.pop('task_completion', None)
    if scope.ctx.event is not None:
        scope.ctx.event._completion_decision = None
    decision = scope.decision()
    if decision is not None:
        result['task_completion'] = decision
        # The core finalizer normally rendered before persisting. Non-core/failed
        # result paths must still receive the same authoritative publication fence.
        result['final_response'] = scope.render(result.get('final_response') or '')
        decision = getattr(scope, 'rendered_decision', decision) if not scope.followup else decision
        result['task_completion'] = decision
        if decision and decision['accepted'] and not scope.followup and scope.ctx.event is not None:
            scope.ctx.event._completion_decision = dict(decision)
    return result



def hook_completion(hook_ctx):
    """Recompute the current owning parent's status at the external hook boundary."""
    if not all(hook_ctx.get(k) for k in ('session_key', 'profile', 'user_id')):
        return {}
    registry = None
    try:
        registry = Registry()
        from gateway.task_completion_managed import remote_snapshot_reader
        ledger = ParentLedger(registry, remote_snapshot=remote_snapshot_reader(registry))
        parent = ledger.current(session_key=hook_ctx['session_key'],
            profile=hook_ctx['profile'], owner=str(hook_ctx['user_id']))
        from gateway.task_completion_modes import effective_mode, publication_decision
        from hermes_cli.config import load_config_readonly
        mode = effective_mode(registry, hook_ctx['profile'], load_config_readonly(), parent)
        return {'task_completion': publication_decision(ledger.decision(parent['parent_id']), mode)} if parent else {}
    except Exception as exc:
        logger.warning('Task completion hook unavailable', exc_info=True)
        return {'task_completion': dict(accepted=False, status='unavailable', missing=[str(exc)])}
    finally:
        if registry is not None:
            registry.close()
