"""Authenticated additive requirement intake shared by admission and restored turns."""

from agent.task_lifecycle.parent import ParentLedger
from agent.task_lifecycle.registry import Registry
from gateway.task_completion_modes import effective_mode, remember_parent_mode


def update_parent(ledger, identity, request, mode, *, previous_accepted=False):
    """Only host-authenticated source messages may extend the durable objective."""
    from gateway.task_completion_requests import adds_requirement
    parent = ledger.current(**identity)
    if not request or not adds_requirement(request.text, parent):
        return parent
    parent = ledger.intake_requirement(**identity, origin=request.origin,
        source_text=request.text, previous_accepted=previous_accepted)
    remember_parent_mode(ledger.registry, parent['parent_id'], mode)
    return parent


def admit_requirement(runner, event):
    """Invalidate a pending completion before its next send, even if this turn queues."""
    from gateway.task_completion import AuthenticatedRequest
    request = getattr(event, '_completion_request', None)
    if not isinstance(request, AuthenticatedRequest) or event.source.is_bot:
        return
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config_readonly
    multiplex = getattr(getattr(runner, 'config', None), 'multiplex_profiles', False)
    token = set_hermes_home_override(str(runner._resolve_profile_home_for_source(event.source))) if multiplex else None
    registry = None
    try:
        registry = Registry()
        ledger = ParentLedger(registry)
        identity = dict(session_key=runner._session_key_for_source(event.source),
            profile=request.profile, owner=request.owner)
        parent = ledger.current(**identity)
        # An already issued completion starts a separate objective. A pending
        # contract is strengthened atomically before a concurrent decision can publish.
        issued = False
        if parent:
            from agent.task_lifecycle.registry import _decoded
            row = registry._conn.execute('SELECT evidence FROM lifecycle_parent_decisions '
                'WHERE parent_id=? ORDER BY recorded_at DESC LIMIT 1', (parent['parent_id'],)).fetchone()
            issued = bool(row and _decoded(row[0]).get('accepted')
                and _decoded(row[0]).get('contract_digest') == parent['contract_digest'])
        mode = effective_mode(registry, request.profile, load_config_readonly(), parent)
        update_parent(ledger, identity, request, mode, previous_accepted=issued)
    finally:
        if registry is not None:
            registry.close()
        if token is not None:
            reset_hermes_home_override(token)
