"""Regression coverage for retired foreign-generation FTS objects."""

import sqlite3
import time

from hermes_state import (
    FTS_STORAGE_VERSION,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    SessionDB,
)


_V2_TRIGGERS = (
    "messages_fts_v2_insert",
    "messages_fts_v2_delete",
    "messages_fts_v2_update",
)


def _build_legacy_v22_db(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.executescript(
        """
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (
                new.id,
                COALESCE(new.content, '') || ' ' ||
                COALESCE(new.tool_name, '') || ' ' ||
                COALESCE(new.tool_calls, '')
            );
        END;
        CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
        END;
        CREATE TRIGGER messages_fts_update
        AFTER UPDATE OF content, tool_name, tool_calls ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
            INSERT INTO messages_fts(rowid, content) VALUES (
                new.id,
                COALESCE(new.content, '') || ' ' ||
                COALESCE(new.tool_name, '') || ' ' ||
                COALESCE(new.tool_calls, '')
            );
        END;

        CREATE VIRTUAL TABLE messages_fts_trigram
        USING fts5(content, tokenize='trigram');
        CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts_trigram(rowid, content) VALUES (
                new.id,
                COALESCE(new.content, '') || ' ' ||
                COALESCE(new.tool_name, '') || ' ' ||
                COALESCE(new.tool_calls, '')
            );
        END;
        CREATE TRIGGER messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts_trigram WHERE rowid = old.id;
        END;
        CREATE TRIGGER messages_fts_trigram_update
        AFTER UPDATE OF content, tool_name, tool_calls ON messages BEGIN
            DELETE FROM messages_fts_trigram WHERE rowid = old.id;
            INSERT INTO messages_fts_trigram(rowid, content) VALUES (
                new.id,
                COALESCE(new.content, '') || ' ' ||
                COALESCE(new.tool_name, '') || ' ' ||
                COALESCE(new.tool_calls, '')
            );
        END;
        """
    )
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
    )
    conn.execute(
        "INSERT INTO state_meta(key, value) VALUES "
        "('fts_optimize_available', '1')"
    )
    conn.execute(
        "INSERT INTO sessions(id, source, started_at) VALUES ('s1', 'cli', ?)",
        (time.time(),),
    )
    for role, content in (
        ("user", "historicalneedle deployment notes"),
        ("assistant", "historicalneedle response"),
        ("tool", "historicalneedle tool payload"),
    ):
        conn.execute(
            "INSERT INTO messages(session_id, timestamp, role, content) "
            "VALUES ('s1', ?, ?, ?)",
            (time.time(), role, content),
        )
    conn.commit()
    conn.close()


def _add_retired_v2_with_unavailable_tokenizer(db_path) -> None:
    """Create a valid v2 index, then poison only its stored tokenizer DDL."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE VIRTUAL TABLE messages_fts_v2 USING fts5(
            content, tool_name, tool_calls, tokenize='unicode61'
        );
        CREATE TRIGGER messages_fts_v2_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts_v2(rowid, content, tool_name, tool_calls)
            VALUES (new.id, new.content, new.tool_name, new.tool_calls);
        END;
        CREATE TRIGGER messages_fts_v2_delete AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts_v2 WHERE rowid = old.id;
        END;
        CREATE TRIGGER messages_fts_v2_update
        AFTER UPDATE OF content, tool_name, tool_calls ON messages BEGIN
            DELETE FROM messages_fts_v2 WHERE rowid = old.id;
            INSERT INTO messages_fts_v2(rowid, content, tool_name, tool_calls)
            VALUES (new.id, new.content, new.tool_name, new.tool_calls);
        END;
        INSERT INTO messages_fts_v2(rowid, content, tool_name, tool_calls)
        SELECT id, content, tool_name, tool_calls FROM messages;
        INSERT INTO state_meta(key, value) VALUES ('fts_v2_ready', '1');
        INSERT INTO state_meta(key, value)
        SELECT 'fts_v2_backfill_snapshot_max', CAST(MAX(id) AS TEXT)
        FROM messages;
        INSERT INTO state_meta(key, value)
        VALUES ('fts_v2_backfill_next_lo', '1');
        """
    )
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql = replace(sql, ?, ?) "
        "WHERE type = 'table' AND name = 'messages_fts_v2'",
        ("tokenize='unicode61'", "tokenize='missing_test_tokenizer'"),
    )
    conn.execute("PRAGMA writable_schema=RESET")
    conn.commit()
    conn.close()


def _add_live_cjk_sibling(db_path) -> None:
    """Add the current-generation shape with a built-in test tokenizer."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE VIEW messages_fts_cjk_src AS
            SELECT id, role, content, tool_name, tool_calls
            FROM messages WHERE role <> 'tool';
        CREATE VIRTUAL TABLE messages_fts_cjk USING fts5(
            content, tool_name, tool_calls,
            content='messages_fts_cjk_src', content_rowid='id',
            tokenize='unicode61'
        );
        CREATE TRIGGER messages_fts_cjk_insert AFTER INSERT ON messages
        WHEN new.role <> 'tool' BEGIN
            INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
            VALUES (new.id, new.content, new.tool_name, new.tool_calls);
        END;
        CREATE TRIGGER messages_fts_cjk_delete AFTER DELETE ON messages
        WHEN old.role <> 'tool' BEGIN
            INSERT INTO messages_fts_cjk(
                messages_fts_cjk, rowid, content, tool_name, tool_calls
            ) VALUES (
                'delete', old.id, old.content, old.tool_name, old.tool_calls
            );
        END;
        CREATE TRIGGER messages_fts_cjk_update
        AFTER UPDATE OF content, tool_name, tool_calls, role ON messages BEGIN
            INSERT INTO messages_fts_cjk(
                messages_fts_cjk, rowid, content, tool_name, tool_calls
            )
            SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls
            WHERE old.role <> 'tool';
            INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
            SELECT new.id, new.content, new.tool_name, new.tool_calls
            WHERE new.role <> 'tool';
        END;
        INSERT INTO messages_fts_cjk(rowid, content, tool_name, tool_calls)
        SELECT id, content, tool_name, tool_calls
        FROM messages WHERE role <> 'tool';
        """
    )
    conn.commit()
    conn.close()


def _object_names(conn, prefix: str) -> set:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE ?", (prefix + "%",)
        )
    }


def test_demote_only_stages_exact_legacy_generation(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _build_legacy_v22_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE VIRTUAL TABLE messages_fts_future USING fts5(content)"
    )
    conn.commit()
    sibling_before = _object_names(conn, "messages_fts_future")
    conn.close()

    monkeypatch.setenv(
        "HERMES_FTS5_CJK_SO", str(tmp_path / "unavailable-cjk-extension.so")
    )
    db = SessionDB(db_path=db_path)
    try:
        db._demote_legacy_fts_to_trash()
        sibling_after = _object_names(db._conn, "messages_fts_future")
        assert sibling_after == sibling_before
        assert not any(
            name.startswith("fts_v22_trash_messages_fts_future")
            for name in _object_names(db._conn, "fts_v22_trash_")
        )
    finally:
        db.close()


def test_optimize_retires_poisoned_v2_but_preserves_live_cjk(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    _build_legacy_v22_db(db_path)
    _add_live_cjk_sibling(db_path)
    _add_retired_v2_with_unavailable_tokenizer(db_path)
    monkeypatch.setenv(
        "HERMES_FTS5_CJK_SO", str(tmp_path / "unavailable-cjk-extension.so")
    )

    db = SessionDB(db_path=db_path)
    try:
        assert db._fts_cjk_loaded is False
        db._FTS_REBUILD_MIN_PAUSE = 0
        db._FTS_REBUILD_DUTY_FACTOR = 0
        result = db.optimize_fts_storage(vacuum=False)

        assert result["ok"] is True
        assert db.get_meta("fts_storage_version") == str(FTS_STORAGE_VERSION)
        assert db.search_messages("historicalneedle")
        assert db._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM messages_fts_docsize)"
        ).fetchone()[0] == 1
        assert _object_names(db._conn, "messages_fts_v2") == set()
        for trigger in _V2_TRIGGERS:
            assert db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (trigger,),
            ).fetchone() is None
        assert db._conn.execute(
            "SELECT 1 FROM state_meta WHERE key LIKE 'fts_v2_%'"
        ).fetchone() is None

        cjk_names = _object_names(db._conn, "messages_fts_cjk")
        assert "messages_fts_cjk" in cjk_names
        assert "messages_fts_cjk_data" in cjk_names
        assert set(_V2_TRIGGERS).isdisjoint(cjk_names)
        assert {
            "messages_fts_cjk_insert",
            "messages_fts_cjk_delete",
            "messages_fts_cjk_update",
        }.issubset(cjk_names)
    finally:
        db.close()


def test_orphaned_v2_alone_is_offered_and_retired(tmp_path, monkeypatch):
    """A compact-layout DB still offers cleanup for the retired generation."""
    db_path = tmp_path / "state.db"
    seeded = SessionDB(db_path=db_path)
    try:
        seeded.create_session("s1", source="cli")
        seeded.append_message(
            "s1", role="user", content="standalone orphan historicalneedle"
        )
    finally:
        seeded.close()
    _add_retired_v2_with_unavailable_tokenizer(db_path)
    monkeypatch.setenv(
        "HERMES_FTS5_CJK_SO", str(tmp_path / "unavailable-cjk-extension.so")
    )

    db = SessionDB(db_path=db_path)
    try:
        assert db._db_has_legacy_inline_fts(db._conn) is False
        assert db.fts_optimize_available() is True
        db._FTS_REBUILD_MIN_PAUSE = 0
        db._FTS_REBUILD_DUTY_FACTOR = 0

        result = db.optimize_fts_storage(vacuum=False)

        assert result["ok"] is True
        assert db.fts_optimize_available() is False
        assert _object_names(db._conn, "messages_fts_v2") == set()
        assert db.search_messages("historicalneedle")
    finally:
        db.close()
