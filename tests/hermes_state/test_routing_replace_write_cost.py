"""Full routing replacement remains atomic without rewriting unchanged rows."""

import json
import sqlite3
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    yield instance
    instance.close()


def _route(session_id):
    return json.dumps({"session_id": session_id})


def _audit(db):
    db._conn.executescript("""
        CREATE TEMP TABLE routing_changes (kind TEXT, session_key TEXT);
        CREATE TEMP TRIGGER audit_routing_insert AFTER INSERT ON gateway_routing BEGIN
            INSERT INTO routing_changes VALUES ('insert', NEW.session_key);
        END;
        CREATE TEMP TRIGGER audit_routing_update AFTER UPDATE ON gateway_routing BEGIN
            INSERT INTO routing_changes VALUES ('update', NEW.session_key);
        END;
        CREATE TEMP TRIGGER audit_routing_delete AFTER DELETE ON gateway_routing BEGIN
            INSERT INTO routing_changes VALUES ('delete', OLD.session_key);
        END;
    """)


def test_replace_only_changes_rows_whose_contents_changed(db):
    original = {"stable": _route("stable"), "changed": _route("before"), "removed": _route("removed")}
    db.replace_gateway_routing_entries(original, scope="main")
    db.save_gateway_routing_entry("other", _route("other"), scope="other")
    _audit(db)
    target = {"stable": original["stable"], "changed": _route("after"), "added": _route("added")}

    assert db.replace_gateway_routing_entries(target, scope="main") == set()

    assert db.load_gateway_routing_entries(scope="main") == target
    assert db.load_gateway_routing_entries(scope="other") == {"other": _route("other")}
    assert {tuple(row) for row in db._conn.execute("SELECT kind, session_key FROM routing_changes")} == {
        ("update", "changed"), ("delete", "removed"), ("insert", "added"),
    }


def test_unchanged_retired_route_is_removed_and_failed_replacement_rolls_back(db):
    entries = {"agent:main:discord:thread:old": _route("old"), "healthy": _route("healthy")}
    db.replace_gateway_routing_entries(entries, scope="main")
    db.set_meta("gateway.retired_session.v1.old", "{}")
    assert db.replace_gateway_routing_entries(entries, scope="main") == {"agent:main:discord:thread:old"}
    assert db.load_gateway_routing_entries(scope="main") == {"healthy": entries["healthy"]}

    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        db.replace_gateway_routing_entries({"bad": "{invalid"}, scope="main")
    assert db.load_gateway_routing_entries(scope="main") == {"healthy": entries["healthy"]}
    db.replace_gateway_routing_entries({}, scope="main")
    assert db.load_gateway_routing_entries(scope="main") == {}


def test_replacement_batches_keep_retirement_fences_scoped_across_boundaries(db):
    keys = [f"agent:{'alpha' if i % 2 else 'main'}:discord:thread:{i}" for i in range(450)]
    entries = {key: _route(f"session-{i}") for i, key in enumerate(keys)}
    entries[keys[199]] = entries[keys[200]] = _route("secondary-retired")
    entries[keys[399]] = entries[keys[400]] = _route("launch-retired")
    # The launch-only marker must reject main at 400 while alpha at 399 survives.
    db.set_meta("gateway.retired_session.v1.launch-retired", json.dumps({"own": "main"}))
    db.set_meta("gateway.retired_route.v1.alpha:secondary-retired", "{}")
    db.set_meta("gateway.retired_session.v1.session-0", "{}")
    db.set_meta("gateway.retired_route.v1.alpha:session-449", json.dumps({
        "expires": True, "retired_at": time.time() - 86400 * 365,
    }))

    retired = db.replace_gateway_routing_entries(entries, scope="main")

    assert retired == {keys[0], keys[199], keys[400]}
    expected = {key: value for key, value in entries.items() if key not in retired}
    assert db.load_gateway_routing_entries(scope="main") == expected
    malformed = dict(entries)
    malformed[keys[201]] = "{invalid"
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        db.replace_gateway_routing_entries(malformed, scope="main")
    assert db.load_gateway_routing_entries(scope="main") == expected
