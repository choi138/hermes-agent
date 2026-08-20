"""Schema/handler parity for session_search.

Regression guard for a merge-integration defect: a tool schema property was
declared while the handler function neither accepted it nor exposed a
``**kwargs`` catch-all.  The model could pass the documented argument and the
runtime would silently drop it — the tool advertised a capability it did not
have.

``session_search`` is the concrete case that regressed, but the parity rule is
general, so the check is written against the schema rather than a hardcoded
argument list.
"""

import inspect

from tools.session_search_tool import SESSION_SEARCH_SCHEMA, session_search


def _schema_properties() -> set:
    params = SESSION_SEARCH_SCHEMA.get("parameters", {})
    return set(params.get("properties", {}).keys())


def _accepted_parameters() -> tuple[set, bool]:
    sig = inspect.signature(session_search)
    names = set()
    has_var_keyword = False
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        names.add(name)
    return names, has_var_keyword


def test_every_schema_property_is_accepted_by_the_handler():
    """A declared property the callable cannot accept is a dead contract."""
    declared = _schema_properties()
    accepted, has_var_keyword = _accepted_parameters()

    assert declared, "session_search schema declares no properties"

    if has_var_keyword:
        return

    orphaned = sorted(declared - accepted)
    assert not orphaned, (
        "SESSION_SEARCH_SCHEMA declares properties that session_search() cannot "
        f"accept: {orphaned}. Either wire the argument through or drop it from "
        "the schema — advertising an argument the runtime discards misleads the "
        "model."
    )


def test_schema_properties_are_reachable_through_the_registry_handler():
    """The registered handler must forward each declared property."""
    from tools.registry import registry

    entry = registry.get_entry("session_search")
    assert entry is not None, "session_search is not registered"

    handler_source = inspect.getsource(entry.handler)
    declared = _schema_properties()

    unforwarded = sorted(
        name for name in declared if f'"{name}"' not in handler_source
    )
    assert not unforwarded, (
        "registered session_search handler never reads these declared schema "
        f"properties: {unforwarded}"
    )
