"""Disclosure preserves the immutable parent's original skill bytes and links."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "fixtures" / "first_response_skills" / "manifest.json"
NAMES = ("hermes-agent", "agent-performance-engineering")


def fenced_json(path):
    return json.loads(re.search(r"```json\n(.*?)\n```", path.read_text(), re.S)[1])


def frontmatter(raw):
    assert raw.startswith(b"---\n")
    return yaml.safe_load(raw.split(b"---\n", 2)[1])


@pytest.mark.parametrize("name", NAMES)
def test_skill_verbatim_coverage_against_immutable_parent(name):
    baseline = json.loads(BASELINE.read_text())[name]
    # The byte-identical immutable fixture is separate from the post-edit skill.
    original = (BASELINE.parent / baseline["path"]).read_bytes()
    assert hashlib.sha256(original).hexdigest() == baseline["sha256"]
    assert len(original) == baseline["bytes"]
    folder = ROOT / "profile-skills" / name
    root = (folder / "SKILL.md").read_bytes()
    assert len(root) <= 12000
    assert frontmatter(root) == frontmatter(original)
    index = fenced_json(folder / "references" / "preservation-index.md")
    assert index["original_sha256"] == baseline["sha256"]
    assert index["original_bytes"] == len(original)
    cursor, recovered = 0, []
    for section in index["sections"]:
        assert section["original_start"] == cursor
        end = section["original_end"]
        assert cursor < end <= len(original)
        target = folder / section["file"]
        assert target.resolve().is_relative_to(folder.resolve())
        chunk = target.read_bytes()[section["start"]:section["end"]]
        assert chunk == original[cursor:end], section["title"]
        recovered.append(chunk)
        cursor = end
    assert cursor == len(original)
    assert b"".join(recovered) == original
    assert hashlib.sha256(b"".join(recovered)).hexdigest() == baseline["sha256"]


@pytest.mark.parametrize("name", NAMES)
def test_skill_local_links_and_targeted_load_are_bounded(name):
    folder = ROOT / "profile-skills" / name
    root = folder / "SKILL.md"
    index = fenced_json(folder / "references" / "preservation-index.md")
    files = {folder / item["file"] for item in index["sections"]} | {root}
    for path in files:
        text = path.read_text()
        # Original links retain their skill-directory base, explicitly documented in each detail.
        targets = re.findall(r"\]\(([^)]+)\)", text)
        targets += re.findall(r"(?<![\w/])(references/[\w./-]+\.md(?:#[\w-]+)?)", text)
        for target in targets:
            target = unquote(target.strip("<>"))
            if "://" in target or target.startswith("#"):
                continue
            relative, _, anchor = target.partition("#")
            destination = folder / relative
            assert destination.is_file(), (path, target)
            if anchor:
                headings = re.findall(r"^#{1,6}\s+(.+)$", destination.read_text(), re.M)
                slugs = {re.sub(r"[^\w\s-]", "", heading.lower()).replace(" ", "-")
                         for heading in headings}
                assert anchor in slugs, (path, target)
    baseline = json.loads(BASELINE.read_text())[name]
    selected = folder / "references" / ("detail-03.md" if name == "hermes-agent" else "detail-01.md")
    assert root.stat().st_size + selected.stat().st_size < baseline["bytes"]
    # Every detail is directly discoverable by topic from the root, not a full-skill reload.
    for file in {item["file"] for item in index["sections"]} - {"SKILL.md"}:
        assert f"]({file})" in root.read_text()
