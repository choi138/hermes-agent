"""Generated metadata remains writable when the core is a sealed input."""
import stat
import tomllib

import pytest

from pm import workspace


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("with_member", [False, True])
def test_generated_manifest_can_change_without_unsealing_core(tmp_path, with_member):
    core = tmp_path / "core"
    core.mkdir()
    manifest = core / "pyproject.toml"
    original = ('[project]\nname="sealed-core"\nversion="1"\n'
                '[tool.uv]\nexclude-newer="3 days"\n')
    manifest.write_text(original, encoding="utf-8")
    manifest.chmod(0o444)
    core.chmod(0o500)
    members = []
    if with_member:
        member = tmp_path / "member"
        member.mkdir()
        (member / "pyproject.toml").write_text(
            '[project]\nname="buildable-member"\nversion="1"\n'
            '[build-system]\nrequires=[]\nbuild-backend="example_backend"\n',
            encoding="utf-8",
        )
        members.append(member)

    root = tmp_path / "workspace"
    workspace._generate_pyproject(members, root, source=core)

    generated = root / "pyproject.toml"
    metadata = tomllib.loads(generated.read_text(encoding="utf-8"))
    assert metadata["project"]["name"] == "sealed-core"
    assert "exclude-newer" not in metadata["tool"]["uv"]
    assert bool(metadata["tool"]["uv"].get("workspace")) == with_member
    assert stat.S_IMODE(generated.stat().st_mode) & stat.S_IWUSR
    assert manifest.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o444
    assert stat.S_IMODE(core.stat().st_mode) == 0o500
