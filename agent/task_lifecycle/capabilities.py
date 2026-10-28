"""Version negotiation for trusted gateway-to-Mac execution transport."""
from .types import LifecycleError

PROTOCOL = 'hermes-task-lifecycle'
SCHEMA_VERSION = 1
ACTIONS = ('capabilities', 'import-request', 'export-result', 'status', 'receipt', 'cancel', 'verify')


def capabilities():
    """Advertise execution support; a Mac receipt never certifies its parent."""
    from .directory_handoff import require_support
    try:
        require_support()
    except LifecycleError as exc:
        supported, reason = False, str(exc)
    else:
        supported, reason = True, None
    return dict(protocol=PROTOCOL, schema_version=SCHEMA_VERSION,
        actions=list(ACTIONS), execution_supported=supported, unavailable_reason=reason,
        acceptance_scope='child', parent_completion=False,
        cli_sandboxes={'codex': ['read-only', 'workspace-write'], 'claude': ['read-only']})


def require_compatible(receipt):
    """Fail closed before launch when mixed releases cannot preserve the contract."""
    if (not isinstance(receipt, dict) or receipt.get('protocol') != PROTOCOL
            or type(receipt.get('schema_version')) is not int
            or receipt['schema_version'] != SCHEMA_VERSION
            or receipt.get('execution_supported') is not True
            or receipt.get('acceptance_scope') != 'child'
            or receipt.get('parent_completion') is not False
            or not isinstance(receipt.get('actions'), list)
            or any(not isinstance(action, str) for action in receipt['actions'])
            or not set(ACTIONS).issubset(receipt.get('actions', []))
            or receipt.get('cli_sandboxes') != {
                'codex': ['read-only', 'workspace-write'], 'claude': ['read-only']}):
        raise LifecycleError('Mac lifecycle capability/schema mismatch; completion remains unverified')
    return receipt
