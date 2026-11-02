"""Large startup and hot-serve recovery passes must leave the owner loop responsive."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from gateway import run_runtime, session_local_recovery


@pytest.mark.asyncio
@pytest.mark.parametrize("hot_serve", [False, True])
@pytest.mark.parametrize("receipt_status", ["queued", "unknown", "refused", "catchup"])
async def test_local_recovery_allows_heartbeats_between_receipts(
        monkeypatch, tmp_path, hot_serve, receipt_status):
    restored, scheduled, heartbeats = [], [], []
    authority = SimpleNamespace(profile_id=str(tmp_path), _schedule=scheduled.append)
    runner = SimpleNamespace(session_authorities=SimpleNamespace(for_home=lambda _: None),
                             session_control_server=None)

    async def nothing(*args, **kwargs):
        return {}

    async def build(*args, **kwargs):
        return authority

    def restore(_authority, sid):
        restored.append(sid)
        if receipt_status == "refused":
            raise session_local_recovery._ReceiptRefused("storage_unavailable")
        if receipt_status == "catchup":
            raise session_local_recovery._CatchUpPending("storage_unavailable")
        return sid

    monkeypatch.setattr("gateway.session_authorities.owner_scope", lambda _: nullcontext())
    monkeypatch.setattr("gateway.session_authorities.serving_authorities", lambda _: [authority])
    monkeypatch.setattr("gateway.session_hosted_service.ensure_hosted_service", nothing)
    monkeypatch.setattr("gateway.session_hosted_service.start_ready_hosted_services", lambda _: None)
    monkeypatch.setattr("gateway.platforms.webhook_ingress.recover_webhook_finalizations", nothing)
    monkeypatch.setattr(run_runtime, "_build_profile_authority", build)
    monkeypatch.setattr(run_runtime, "_drop_retired_local_adapter", lambda _: None)
    monkeypatch.setattr(run_runtime, "recover_bot_mailbox", nothing)
    monkeypatch.setattr(run_runtime, "_recover_published_api_turns", lambda *_: None)
    monkeypatch.setattr(run_runtime, "_publish_served_set", lambda _: None)
    monkeypatch.setattr(run_runtime, "recover_profile_native_sessions", nothing)
    monkeypatch.setattr(session_local_recovery, "local_receipt_ids", lambda _: list(range(40)))
    monkeypatch.setattr(session_local_recovery, "restore_local_session", restore)
    monkeypatch.setattr(session_local_recovery, "list_session_admissions",
                        lambda *args, **kwargs: [{"status": receipt_status}])
    monkeypatch.setattr("gateway.session_viewer_close.rearm_persisted_closes", lambda _: [])
    authority.db = object()

    async def heartbeat():
        while True:
            heartbeats.append(len(restored))
            await asyncio.sleep(0)

    pulse = asyncio.create_task(heartbeat())
    try:
        if hot_serve:
            await run_runtime.serve_profile_runtime(runner, "secondary", tmp_path)
        else:
            await run_runtime.recover_gateway_native_sessions(runner)
        assert restored == list(range(40))
        assert scheduled == (restored if receipt_status == "queued" else [])
        assert any(0 < count < 40 for count in heartbeats)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_recovery_allows_heartbeats_during_route_scan(monkeypatch):
    resolved, heartbeats = [], []
    entries = [SimpleNamespace(session_id=str(i), origin=object()) for i in range(40)]

    def logical_owner(sid):
        resolved.append(sid)
        return sid

    async def recover(bindings):
        assert [owner for owner, *_ in bindings] == ["39"]
        return {"39": "recovered"}

    authority = SimpleNamespace(
        db=SimpleNamespace(_read_all=lambda _: [{"target_session_id": "39"}]),
        logical_owner=logical_owner, recover_native_sessions=recover, profile_id="secondary")
    runner = SimpleNamespace(session_store=SimpleNamespace(list_sessions=lambda: entries),
                             _adapter_for_source=lambda _: "adapter")
    monkeypatch.setattr("gateway.session_authorities.owner_scope", lambda _: nullcontext())

    async def heartbeat():
        while True:
            heartbeats.append(len(resolved))
            await asyncio.sleep(0)

    pulse = asyncio.create_task(heartbeat())
    try:
        assert await run_runtime.recover_profile_native_sessions(runner, authority) == {"39": "recovered"}
        assert any(0 < count < 40 for count in heartbeats)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
