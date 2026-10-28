"""Short consistent DB snapshots around slow parent verification/readback."""
from .contract import _digest
from .registry import _decoded


def snapshot(registry, parent):
    """Caller holds its connection's short transaction; never perform network I/O."""
    conn = registry._conn
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    executions = conn.execute('SELECT execution_id,parent_id,run_id,attribution '
        'FROM lifecycle_parent_executions WHERE session_key=? AND profile=? AND owner=? '
        'AND (parent_id=? OR parent_id IS NULL) ORDER BY execution_id',
        (parent['session_key'], parent['profile'], parent['owner'], parent['parent_id'])).fetchall()
    links = conn.execute('SELECT execution_id,parent_id,run_id,attribution '
        'FROM lifecycle_parent_execution_links WHERE parent_id=? AND contract_digest=? ORDER BY execution_id',
        (parent['parent_id'], parent['contract_digest'])).fetchall()
    submissions, receipts = [], []
    if 'lifecycle_gateway_submissions' in tables:
        submissions = conn.execute('SELECT submission_id,payload FROM lifecycle_gateway_submissions '
            'WHERE parent_id=? ORDER BY submission_id', (parent['parent_id'],)).fetchall()
    if 'lifecycle_gateway_submission_receipts' in tables:
        receipts = conn.execute('SELECT r.submission_id,r.run_id FROM lifecycle_gateway_submission_receipts r '
            'JOIN lifecycle_gateway_submissions s ON s.submission_id=r.submission_id '
            'WHERE s.parent_id=? ORDER BY r.submission_id', (parent['parent_id'],)).fetchall()
    terminals = conn.execute('SELECT submission_id,parent_id,run_id,evidence '
        'FROM lifecycle_parent_terminal_submissions WHERE parent_id=? ORDER BY submission_id',
        (parent['parent_id'],)).fetchall()
    supersessions = conn.execute('SELECT execution_id,contract_digest,parent_id,run_id,evidence '
        'FROM lifecycle_parent_supersessions WHERE parent_id=? AND contract_digest=? ORDER BY execution_id',
        (parent['parent_id'], parent['contract_digest'])).fetchall()
    run_ids = sorted({row[2] for row in executions + links if row[2]} | {row[1] for row in receipts})
    children = []
    for run_id in run_ids:
        # Delivery is an independent fact. Queueing or recovering an ACK-lost
        # result must preserve a decision for the same accepted artifact.
        events = conn.execute('SELECT id,phase,evidence FROM lifecycle_events WHERE run_id=? '
            "AND phase NOT IN ('delivering','delivery_unconfirmed','delivered') ORDER BY id", (run_id,)).fetchall()
        jobs = conn.execute('SELECT payload,result,cancel_requested FROM lifecycle_jobs WHERE run_id=?', (run_id,)).fetchall()
        verification = []
        if 'lifecycle_verifications' in tables:
            verification = conn.execute('SELECT contract_digest,evidence FROM lifecycle_verifications WHERE run_id=?', (run_id,)).fetchall()
        children.append((run_id, events, jobs, verification))
    return dict(executions=executions, links=links, submissions=submissions, receipts=receipts,
                children=children, terminals=terminals, supersessions=supersessions,
                contract_digest=parent['contract_digest'])


def effective_executions(inputs):
    linked = {row[0]: row for row in inputs['links']}
    return [linked.get(row[0], row) for row in inputs['executions']]


def pending(inputs):
    executions = {row[0]: row for row in effective_executions(inputs)}
    receipts = dict(inputs['receipts'])
    supersessions = {row[0]: row for row in inputs['supersessions']}
    missing = []
    for identity, encoded in inputs['submissions']:
        row = executions.get(identity)
        payload = _decoded(encoded)
        supersession = supersessions.get(identity)
        replaced = bool(row and supersession and row[2] == supersession[3]
            and _decoded(supersession[4])['old_run_id'] == receipts.get(identity)
            and _decoded(row[3]).get('not_before') == _decoded(supersession[4])['not_before'])
        if (not row or not receipts.get(identity) or (row[2] != receipts[identity] and not replaced)
                or _decoded(row[3])['contract_digest'] != inputs['contract_digest']
                or (not replaced and _decoded(row[3])['outcome_ids'] != payload['outcome_ids'])):
            missing.append('pending managed submission:' + identity)
    return missing


def digest(inputs):
    return _digest(inputs)
