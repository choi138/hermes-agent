"""Authenticated readbacks resolved solely through immutable gateway grants."""
import json

from agent.task_lifecycle.types import LifecycleError


def committed_run(registry, run_id):
    row = registry._conn.execute('SELECT s.payload FROM lifecycle_gateway_submissions s '
        'JOIN lifecycle_gateway_submission_receipts r ON r.submission_id=s.submission_id '
        'WHERE r.run_id=?', (run_id,)).fetchone()
    if not row:
        raise LifecycleError('Remote run lacks committed gateway transport')
    return json.loads(row[0])


def remote_status_reader(registry):
    """Terminal proof never relies on a model's status or superseded artifact bytes."""
    def read(run_id):
        from agent.task_lifecycle.transport import SSHExecutor
        payload = committed_run(registry, run_id)
        status = SSHExecutor(**payload['transport']).call('status', run_id)
        if status.get('run_id') != run_id:
            raise LifecycleError('Authenticated remote status identity mismatch')
        return status
    return read
