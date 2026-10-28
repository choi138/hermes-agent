"""Value normalization shared by platform delivery helpers."""


def _platform_name(platform) -> str:
    """Normalize a Platform enum / raw string into a lowercase name."""
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def _or_default(thunk, default, exc=(TypeError, ValueError)):
    """``thunk()``, or ``default`` when it raises one of ``exc`` (numeric config/env coercion)."""
    try:
        return thunk()
    except exc:
        return default
