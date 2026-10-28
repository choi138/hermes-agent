"""Operator execution policy for an authenticated gateway lifecycle request.

This object is constructed by trusted gateway configuration, never from model
arguments. It bounds Mac runner fields without reviving the removed router.
"""
from dataclasses import dataclass

from agent.task_lifecycle.types import LifecycleError


@dataclass(frozen=True)
class GatewayExecutionPolicy:
    lane: str
    workdir: str
    timeout_seconds: int
    permissions: str = 'workspace_write'
    approval: str = 'required'
    host: str = 'mac'
    refusal_reason: str | None = None

    def validate(self, request):
        if (self.lane not in {'codex', 'claude'} or self.host != 'mac'
                or self.refusal_reason or self.approval not in {'required', 'not_required'}
                or self.permissions not in {'read_only', 'workspace_write'}
                or request.get('cli') != self.lane or request.get('workdir') != self.workdir
                or type(request.get('timeout')) is not int
                or not 0 < request['timeout'] <= self.timeout_seconds
                or request.get('sandbox') != ('read-only' if self.permissions == 'read_only' else 'workspace-write')):
            raise LifecycleError('Request exceeds operator execution policy')


def authenticated_scope(source, session_key, request_text):
    from gateway.task_completion import _current
    scope = _current.get()
    if (scope is None or scope.request is None or source is not scope.ctx.source
            or source.is_bot or scope.ctx.event.internal
            or scope.identity['session_key'] != session_key
            or scope.parent is None or request_text != scope.parent['original_request']
            or not any(item['origin'] == scope.request.origin and item['text'] == scope.request.text
                for item in scope.parent['contract']['sources'])
            or str(source.user_id or '') != scope.request.owner
            or str(source.profile or 'default') != scope.request.profile):
        raise LifecycleError('Active authenticated original user turn required')
    return scope
