from plugins.mention_inbox.presentation import normalize_review_text


def test_review_normalizer_removes_badges_html_and_suggested_diff() -> None:
    raw = """
    [![CI](https://img.shields.io/badge/build-passing.svg)](https://ci.example)
    <!-- internal reviewer state -->
    <details><summary>Details</summary>
    Update `level_status` in `content_level_status.model.ts`.
    ```suggestion
    expect(level_status).toBe("UP")
    ```
    </details>
    """

    normalized = normalize_review_text(raw, limit=300)

    assert normalized == (
        "Details Update `level_status` in `content_level_status.model.ts`."
    )
    assert "shields.io" not in normalized
    assert "suggestion" not in normalized
    assert "<details>" not in normalized


def test_review_normalizer_preserves_code_and_neutralizes_mentions() -> None:
    normalized = normalize_review_text(
        "@recent-won check `level_status` and file_name.py",
        limit=200,
    )

    assert normalized == (
        "@\u200brecent-won check `level_status` and file_name.py"
    )


def test_review_normalizer_is_deterministically_bounded() -> None:
    text = "review " * 100

    assert normalize_review_text(text, limit=40) == (
        "review review review review review…"
    )
