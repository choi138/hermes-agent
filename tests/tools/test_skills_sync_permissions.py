"""Installed skill copies must be editable even when their package is sealed."""

import stat

import pytest

from tools import skills_sync
from tools.skills_sync_bundled_ops import ensure_bundled_skill


@pytest.fixture
def packaged_skill(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    source = bundled / "development" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\n---\nPackage v2\n")
    scripts = source / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("#!/bin/sh\necho demo\n")
    skills = tmp_path / "home" / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setattr(skills_sync, "SKILLS_DIR", skills)
    monkeypatch.setattr(skills_sync, "MANIFEST_FILE", skills / ".bundled_manifest")
    monkeypatch.setattr(skills_sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(skills_sync, "_get_optional_dir", lambda: tmp_path / "optional")
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", list)
    entries = [source, *source.rglob("*")]
    for path in entries:
        path.chmod(0o555 if path.is_dir() or path.suffix == ".sh" else 0o444)
    try:
        yield source, skills
    finally:
        for path in entries:
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("operation", ["install", "update", "restore"])
def test_package_copies_allow_edits_and_new_remote_files(packaged_skill, operation):
    source, skills = packaged_skill
    dest = skills / "development" / "demo"
    source_modes = {p: stat.S_IMODE(p.stat().st_mode) for p in [source, *source.rglob("*")]}
    if operation == "update":
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("---\nname: demo\n---\nPackage v1\n")
        skills_sync._write_manifest({"demo": skills_sync.dir_hash(dest)})

    if operation == "restore":
        result = ensure_bundled_skill("demo")
        assert result["ok"] and result["action"] == "restored"
    else:
        result = skills_sync.sync_skills(quiet=True)
        assert "demo" in result["updated" if operation == "update" else "copied"]

    assert skills_sync.dir_hash(dest) == skills_sync.dir_hash(source)
    for path, mode in source_modes.items():
        assert stat.S_IMODE(path.stat().st_mode) == mode
        copy = dest / path.relative_to(source)
        assert stat.S_IMODE(copy.stat().st_mode) == mode | stat.S_IWUSR
    (dest / "references").mkdir()
    (dest / "references" / "returned.md").write_text("Remote result\n")
    (dest / "SKILL.md").write_text("User edit\n")
    assert (dest / "scripts" / "run.sh").stat().st_mode & stat.S_IXUSR


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("operation", ["update", "restore"])
def test_sync_preserves_user_modified_readonly_skill(packaged_skill, operation):
    _, skills = packaged_skill
    dest = skills / "development" / "demo"
    dest.mkdir(parents=True)
    skill = dest / ("SKILL.md" if operation == "update" else "notes.md")
    skill.write_text("---\nname: demo\n---\nInstalled v1\n")
    skills_sync._write_manifest({"demo": skills_sync.dir_hash(dest)})
    skill.write_text("---\nname: demo\n---\nUser-owned version\n")
    skill.chmod(0o444)
    dest.chmod(0o555 if operation == "update" else 0o755)
    try:
        if operation == "update":
            result = skills_sync.sync_skills(quiet=True)
            assert "demo" in result["user_modified"]
        else:
            assert ensure_bundled_skill("demo")["action"] == "restored"
        assert "User-owned version" in skill.read_text()
        assert stat.S_IMODE(skill.stat().st_mode) == 0o444
        assert stat.S_IMODE(dest.stat().st_mode) == (0o555 if operation == "update" else 0o755)
    finally:
        dest.chmod(0o755)
