"""Delivery obligations for verified artifacts; never performs network sends."""

from dataclasses import dataclass, replace
import hashlib

from agent.task_lifecycle.acceptance import AcceptanceGate, Artifact, Verification
from agent.task_lifecycle.types import LifecycleError, Phase, PhaseTransitionError, require_phase


@dataclass(frozen=True)
class Origin:
    session_key: str
    platform: str
    channel_id: str
    thread_id: str | None
    content: str
    adapter_profile: str | None = None


@dataclass(frozen=True)
class TransportReceipt:
    """Transport receipt, not human acknowledgement; never implies user read."""

    message_id: str
    channel_id: str
    content_digest: str


@dataclass(frozen=True)
class Delivery:
    run_id: str
    origin: Origin
    artifact: Artifact
    verification: Verification
    obligation_id: str
    phase: Phase = Phase.DELIVERING
    # transport receipt, not human acknowledgement
    transport_receipt: TransportReceipt | None = None
    reason: str = ''

    @property
    def status(self) -> str:
        return {
            Phase.DELIVERING: '검증 완료 · 전달 중',
            Phase.DELIVERY_UNCONFIRMED: '검증 완료 · 전달 미확인',
            Phase.DELIVERED: '검증 완료 · 전송 확인',
        }[self.phase]


class Handoff:
    """Connect acceptance evidence to the existing delivery ledger.

    ``pending`` exposes this instance's outstanding handoffs. Durable recovery
    remains the gateway ledger's ``sweep_recoverable`` responsibility. The sink
    implements record_obligation, mark_delivered and mark_failed; it sends no
    messages. A receipt is a transport receipt, not human acknowledgement.
    """

    def __init__(self, gate: AcceptanceGate, *, sink=None):
        if sink is None:
            from gateway import delivery_ledger
            sink = delivery_ledger
        self.gate = gate
        self.sink = sink
        self._deliveries: dict[str, Delivery] = {}

    def phase(self, run_id: str) -> Phase:
        delivery = self._deliveries.get(run_id)
        if delivery is not None:
            return delivery.phase
        evidence = self.gate.verified(run_id)
        return evidence.phase if evidence else Phase.ACCEPTED

    def open(self, run_id: str, *, origin: Origin, artifact: Artifact) -> Phase:
        require_phase(self.phase(run_id), at_least=Phase.VERIFIED)
        existing = self._deliveries.get(run_id)
        if existing:
            if existing.origin != origin or existing.artifact != artifact:
                raise LifecycleError('Cannot replace an existing handoff')
            return existing.phase
        evidence = self.gate.verified(run_id)
        if evidence is None or evidence.artifact != artifact:
            raise LifecycleError('Artifact does not match verified evidence')
        from gateway.delivery_ledger import compute_obligation_id
        obligation_id = compute_obligation_id(origin.session_key, run_id, origin.content)
        self.sink.record_obligation(
            obligation_id=obligation_id, session_key=origin.session_key,
            platform=origin.platform, chat_id=origin.channel_id,
            thread_id=origin.thread_id, content=origin.content,
            adapter_profile=origin.adapter_profile,
        )
        self._deliveries[run_id] = Delivery(run_id, origin, artifact, evidence, obligation_id)
        return Phase.DELIVERING

    def _get(self, run_id: str) -> Delivery:
        if run_id not in self._deliveries:
            raise PhaseTransitionError('Delivery has not been opened')
        return self._deliveries[run_id]

    def confirm(self, run_id: str, receipt: TransportReceipt) -> Phase:
        """Record a transport receipt, not human acknowledgement."""
        delivery = self._get(run_id)
        if not isinstance(receipt, TransportReceipt) or not all(
            isinstance(value, str) and value.strip()
            for value in (receipt.message_id, receipt.channel_id, receipt.content_digest)
        ):
            raise LifecycleError('A complete transport receipt is required')
        digest = hashlib.sha256(delivery.origin.content.encode('utf-8')).hexdigest()
        if receipt.channel_id != delivery.origin.channel_id or receipt.content_digest != digest:
            raise LifecycleError('Transport receipt does not match the delivery')
        if delivery.transport_receipt is not None:
            if delivery.transport_receipt != receipt:
                raise LifecycleError('Delivery already has a different transport receipt')
            return delivery.phase
        self.sink.mark_delivered(delivery.obligation_id)
        self._deliveries[run_id] = replace(delivery, phase=Phase.DELIVERED,
                                           transport_receipt=receipt)
        return Phase.DELIVERED

    def mark_unconfirmed(self, run_id: str, reason: str) -> Phase:
        delivery = self._get(run_id)
        if delivery.phase is Phase.DELIVERED:
            raise PhaseTransitionError('Confirmed delivery cannot regress')
        self.sink.mark_failed(delivery.obligation_id, reason)
        self._deliveries[run_id] = replace(delivery, phase=Phase.DELIVERY_UNCONFIRMED,
                                           reason=reason)
        return Phase.DELIVERY_UNCONFIRMED

    def pending(self) -> list[Delivery]:
        return [delivery for delivery in self._deliveries.values()
                if delivery.phase is not Phase.DELIVERED]


# Durable opt-in finalizer. The generic Handoff API above remains available to
# existing consumers; the runnable workflow uses these profile-scoped records.
def _durable_schema(registry):
    registry._conn.execute('''CREATE TABLE IF NOT EXISTS lifecycle_handoffs (
        obligation_id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
        payload TEXT NOT NULL, message_id TEXT, receipt TEXT
    )''')
    registry._conn.commit()


def lifecycle_obligation(obligation_id):
    """No schema mutation/import-heavy registry initialization for ordinary sends."""
    import sqlite3
    from contextlib import closing
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / 'state.db'
    if not path.exists():
        return False
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_handoffs'").fetchone():
            return False
        return conn.execute('SELECT 1 FROM lifecycle_handoffs WHERE obligation_id=?', (obligation_id,)).fetchone() is not None


def queue_result(run_id, content, *, attachments=()):
    """Queue a verified result on its immutable original destination; no send."""
    import json
    from pathlib import Path
    from .registry import Registry
    from .verification import evidence, snapshot, run_lock, read_artifact
    from .workflow import write_private
    from gateway import delivery_ledger
    with run_lock(run_id):
        registry = Registry()
        try:
            run, job = registry.lookup(run_id), registry.job(run_id)
            grant = job['payload']
            verified = evidence(registry, run_id)
            if not verified or not verified['accepted'] or run.phase not in {
                    Phase.VERIFIED, Phase.DELIVERING, Phase.DELIVERY_UNCONFIRMED, Phase.DELIVERED}:
                raise LifecycleError('Current accepted verification is required')
            if snapshot(run.contract.workdir, grant['artifacts']) != verified['artifact']:
                raise LifecycleError('Artifact changed after verification')
            if not isinstance(content, str) or not content.strip() or len(content) > 1750:
                raise LifecycleError('Final text must fit one transport message (1–1750 characters)')
            if len(attachments) > 1 or any(p not in grant['artifacts'] for p in attachments):
                raise LifecycleError('At most one contract-verified attachment is supported per result')
            destination = grant['destination']
            if not destination or destination['platform'] != 'discord':
                raise LifecycleError('Only an original Discord destination has verified transport support')
            # Visible correlation makes an ACK-loss recoverable by readback;
            # inability to find it never licenses an automatic second send.
            text = content + f'\n\n참조: {run_id}'
            oid = delivery_ledger.compute_obligation_id(destination['session_key'], run_id, text)
            copied = []
            for relative in attachments:
                source = Path(run.contract.workdir) / relative
                expected = verified['artifact']['files'][relative]
                data = read_artifact(run.contract.workdir, relative, expected)
                target = Path(grant['request']['output_dir']) / f'{run_id}-{source.name}'
                if target.exists():
                    if target.is_symlink() or target.read_bytes() != data:
                        raise LifecycleError('Attachment snapshot conflict')
                else:
                    write_private(target, data)
                copied.append(dict(path=str(target), name=source.name, bytes=len(data), sha256=expected['sha256']))
            payload = dict(destination=destination, content=text, attachments=copied,
                           artifact_revision=verified['artifact']['revision'])
            _durable_schema(registry)
            encoded = json.dumps(payload, sort_keys=True)
            with registry._conn:
                row = registry._conn.execute('SELECT obligation_id,payload FROM lifecycle_handoffs WHERE run_id=?', (run_id,)).fetchone()
                if row and row != (oid, encoded):
                    raise LifecycleError('Final result is immutable; cannot replace destination/content')
                registry._conn.execute('INSERT OR IGNORE INTO lifecycle_handoffs(obligation_id,run_id,payload) VALUES(?,?,?)',
                                       (oid, run_id, encoded))
            delivery_ledger.record_obligation(obligation_id=oid, session_key=destination['session_key'],
                platform=destination['platform'], chat_id=destination['channel_id'], thread_id=destination['thread_id'],
                content=text, adapter_profile=destination['adapter_profile'], preserve_existing=True)
            if run.phase is Phase.VERIFIED:
                registry.record_phase(run_id, Phase.DELIVERING, evidence={'delivery_ref':oid})
            return oid
        finally:
            registry.close()


async def deliver_result(obligation_id, adapter, *, adapter_profile):
    """Called by the existing gateway finalizer/recovery with its owned adapter.

    Sent message ID + exact content/attachment bytes must be read back. ACK
    loss remains ambiguous unless the same result is found on the origin.
    No automatic resubmit and no claim that a human read the result.
    """
    import json
    from .registry import Registry
    from .verification import run_lock
    from gateway import delivery_ledger
    registry = Registry()
    try:
        _durable_schema(registry)
        row = registry._conn.execute('SELECT run_id,payload,message_id,receipt FROM lifecycle_handoffs WHERE obligation_id=?', (obligation_id,)).fetchone()
        if row is None:
            raise LifecycleError('Unknown lifecycle delivery')
        run_id, encoded, message_id, confirmed = row
        payload = json.loads(encoded)
        dest = payload['destination']
        if adapter_profile != dest['adapter_profile']:
            raise LifecycleError('Adapter profile does not own the origin')
        with run_lock(run_id):
            # Another process may have finalized between the first lookup
            # and lock acquisition. Never act on a stale unsent snapshot.
            encoded, message_id, confirmed = registry._conn.execute(
                'SELECT payload,message_id,receipt FROM lifecycle_handoffs WHERE obligation_id=?',
                (obligation_id,)).fetchone()
            payload = json.loads(encoded)
            if confirmed:
                receipt = json.loads(confirmed)
                phase = registry.lookup(run_id).phase
                if phase is not Phase.DELIVERED:
                    registry.record_phase(run_id, Phase.DELIVERED, evidence=receipt)
                delivery_ledger.mark_delivered(obligation_id)
                return True
            run = registry.lookup(run_id)
            if run.phase in {Phase.VERIFIED, Phase.DELIVERY_UNCONFIRMED}:
                registry.record_phase(run_id, Phase.DELIVERING, evidence={'delivery_ref':obligation_id})
            target = str(dest['thread_id'] or dest['channel_id'])
            try:
                if not hasattr(adapter, 'read_lifecycle_result') or not hasattr(adapter, 'send_lifecycle_result'):
                    raise LifecycleError('Adapter cannot send and read back an exact lifecycle result')
                # The ledger may have been claimed after a process died. Its
                # send intent (message_id="pending") is saved before network I/O.
                if message_id == 'pending':
                    message_id = await adapter.find_lifecycle_result(target, payload['content'])
                    if not message_id:
                        raise LifecycleError('Previous send is ambiguous; readback did not find it')
                if message_id is None:
                    attachment = None
                    if payload['attachments']:
                        item = payload['attachments'][0]
                        from .workflow import read_private
                        data = read_private(item['path'], limit=8*1024*1024)
                        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                            raise LifecycleError('Private attachment snapshot changed')
                        attachment = {'name': item['name'], 'data': data}
                    with registry._conn:
                        registry._conn.execute('UPDATE lifecycle_handoffs SET message_id=? WHERE obligation_id=?', ('pending', obligation_id))
                    delivery_ledger.mark_attempting(obligation_id)
                    result = await adapter.send_lifecycle_result(chat_id=target, content=payload['content'],
                                                                 attachment=attachment)
                    if not result.success or not result.message_id:
                        raise LifecycleError('Transport did not confirm a message ID')
                    message_id = str(result.message_id)
                with registry._conn:
                    registry._conn.execute('UPDATE lifecycle_handoffs SET message_id=? WHERE obligation_id=?', (message_id, obligation_id))
                receipt = await adapter.read_lifecycle_result(target, message_id)
                expected_attachments = [{k:a[k] for k in ('name','bytes','sha256')} for a in payload['attachments']]
                if (receipt.get('message_id') != message_id or receipt.get('channel_id') != target
                        or receipt.get('content_digest') != hashlib.sha256(payload['content'].encode()).hexdigest()
                        or receipt.get('attachments') != expected_attachments):
                    raise LifecycleError('Original thread/content/attachment readback mismatch')
                with registry._conn:
                    registry._conn.execute('UPDATE lifecycle_handoffs SET receipt=? WHERE obligation_id=?', (json.dumps(receipt, sort_keys=True), obligation_id))
                registry.record_phase(run_id, Phase.DELIVERED, evidence=receipt)
                delivery_ledger.mark_delivered(obligation_id)
                return True
            except Exception as exc:
                delivery_ledger.mark_failed(obligation_id, type(exc).__name__)
                registry.record_phase(run_id, Phase.DELIVERY_UNCONFIRMED,
                                      evidence={'reason':type(exc).__name__})
                return False
    finally:
        registry.close()
