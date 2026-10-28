"""Trusted profile rollout policy; guarded profiles cannot silently downgrade."""


def _schema(registry):
    with registry._lock, registry._conn:
        for table, columns in (
                ('profile', 'profile TEXT PRIMARY KEY, mode TEXT NOT NULL'),
                ('parent', 'parent_id TEXT PRIMARY KEY, mode TEXT NOT NULL')):
            name = 'lifecycle_completion_' + table + '_policy'
            registry._conn.execute(f'CREATE TABLE IF NOT EXISTS {name} ({columns})')
            for action in ('UPDATE', 'DELETE'):
                registry._conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {name}_no_{action.lower()}
                    BEFORE {action} ON {name} BEGIN
                    SELECT RAISE(ABORT, 'completion protection policy is append-only'); END''')


def effective_mode(registry, profile, config, parent=None):
    """Only trusted config can activate protection; persisted activation survives rollback."""
    _schema(registry)
    gate = config.get('completion_gate', {}) if isinstance(config, dict) else {}
    requested = gate.get('mode', 'support') if isinstance(gate, dict) else 'support'
    requested = requested if requested in {'canary', 'enforce'} else 'support'
    with registry._lock, registry._conn:
        prior = registry._conn.execute('SELECT mode FROM lifecycle_completion_profile_policy '
            'WHERE profile=?', (profile,)).fetchone()
        parent_mode = None
        if parent:
            row = registry._conn.execute('SELECT mode FROM lifecycle_completion_parent_policy '
                'WHERE parent_id=?', (parent['parent_id'],)).fetchone()
            # Pre-policy contracts already guarded by an older installed gate stay guarded.
            parent_mode = row[0] if row else 'enforce'
        mode = prior[0] if prior else (parent_mode if parent_mode != 'support' else requested)
        mode = mode or requested
        if mode in {'canary', 'enforce'}:
            registry._conn.execute('INSERT OR IGNORE INTO lifecycle_completion_profile_policy VALUES(?,?)',
                                   (profile, mode))
    return mode


def remember_parent_mode(registry, parent_id, mode):
    _schema(registry)
    with registry._lock, registry._conn:
        registry._conn.execute('INSERT OR IGNORE INTO lifecycle_completion_parent_policy VALUES(?,?)',
                               (parent_id, mode))


def publication_decision(decision, mode):
    """Support observes evidence but never grants certified parent completion."""
    if decision is None or mode != 'support':
        return decision
    return dict(decision, accepted=False, status='unverified', mode='support',
        missing=list(decision.get('missing', [])) + ['profile support mode: parent completion not certified'])
