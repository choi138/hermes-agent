"""Resolve process-report presentation from the owning turn's completion policy."""
import logging

from gateway.task_completion_delivery import execution_report, strip_task_markers

logger = logging.getLogger(__name__)


def context_report(ctx, report):
    from agent.task_lifecycle.registry import Registry
    from agent.task_lifecycle.parent import ParentLedger
    from gateway.task_completion_managed import remote_snapshot_reader
    from gateway.task_completion_modes import effective_mode
    registry = None
    try:
        registry = Registry()
        ledger = ParentLedger(registry, remote_snapshot=remote_snapshot_reader(registry))
        profile = str(ctx.source.profile or 'default')
        parent = ledger.current(session_key=ctx.session_key or '', profile=profile,
                                owner=str(ctx.source.user_id or ''))
        mode = effective_mode(registry, profile, getattr(ctx, 'user_config', None) or {}, parent)
        return strip_task_markers(report) if mode == 'support' else execution_report(report)
    except Exception:
        logger.warning('Process report completion mode unavailable', exc_info=True)
        return execution_report(report)
    finally:
        if registry is not None:
            registry.close()
