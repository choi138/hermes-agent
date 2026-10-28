"""Pure eligibility checks for bounded startup turn restoration."""
from typing import Optional


def _orphaned_active_turn(entry, boot_id: Optional[str]) -> Optional[dict]:
    """Return a turn eligible for bounded resume on this boot."""
    record = getattr(entry, "active_turn", None)
    if not isinstance(record, dict):
        return None
    if record.get("status") == "retry_wait":
        return record
    if record.get("status") == "interrupted":
        return record
    if record.get("status") in {"running", "resuming"} and record.get("boot_id") != boot_id:
        return record
    return None
