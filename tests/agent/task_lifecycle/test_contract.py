from dataclasses import FrozenInstanceError, replace

import pytest


def contract(**changes):
    from agent.task_lifecycle.contract import TaskContract

    values = dict(request_text="Implement the fix", objective="Fix lifecycle",
                  allowed_paths=("/tmp/work",), forbidden_actions=("push",),
                  acceptance_checks=("pytest passes",), requires_approval=False,
                  origin="discord:123", owner="alice", request_revision="1",
                  profile="default", repo_root="/tmp/work", workdir="/tmp/work")
    values.update(changes)
    return TaskContract(**values)


def test_frozen_and_canonical_digest():
    task = contract()
    with pytest.raises(FrozenInstanceError):
        task.owner = "bob"
    assert len(task.digest()) == 64
    assert task.digest() == contract(**dict(reversed(list(task.__dict__.items())))).digest()


@pytest.mark.parametrize("field,value", [
    ("request_text", "Implement another fix"), ("objective", "Other goal"),
    ("allowed_paths", ("/tmp",)), ("forbidden_actions", ("commit",)),
    ("acceptance_checks", ("integration passes",)), ("requires_approval", True),
    ("origin", "discord:456"), ("owner", "bob"),
])
def test_digest_covers_every_field(field, value):
    task = contract()
    changes = {field: value}
    if field == "requires_approval":
        changes["approval_ref"] = "approval:1"
    assert replace(task, **changes).digest() != task.digest()


def test_key_depends_on_request_identity():
    task = contract()
    assert task.idempotency_key() == contract().idempotency_key()
    assert task.idempotency_key() == replace(task, owner="bob").idempotency_key()
    assert task.idempotency_key() != replace(task, origin="discord:other").idempotency_key()
    assert task.idempotency_key() != replace(task, objective="other", request_revision="2").idempotency_key()
    assert task.idempotency_key() == replace(task, objective="other").idempotency_key()


@pytest.mark.parametrize("changes", [
    {"objective": " "}, {"origin": ""}, {"owner": "\t"},
    {"allowed_paths": ("relative/path",)}, {"allowed_paths": ("/tmp/../work",)},
    {"acceptance_checks": ()}, {"acceptance_checks": (" ",)},
    {"allowed_paths": ["/tmp/work"]}, {"requires_approval": "false"},
])
def test_invalid_contract_fails_closed(changes):
    from agent.task_lifecycle.types import LifecycleError

    with pytest.raises(LifecycleError):
        contract(**changes)


@pytest.mark.parametrize("text", ["What is a lifecycle?", "Explain this function", "현재 상태 알려줘", "이 함수 설명해줘"])
def test_lightweight_request_bypasses_lifecycle(text):
    from agent.task_lifecycle.contract import TaskContract

    assert TaskContract.from_request(text, origin="discord:1", owner="alice") is None


def test_action_request_builds_validated_contract():
    from agent.task_lifecycle.contract import TaskContract

    task = TaskContract.from_request("Fix the lifecycle bug", origin="discord:1", owner="alice",
                                    request_revision="1", profile="default",
                                    repo_root="/tmp/work", workdir="/tmp/work",
                                    allowed_paths=("/tmp/work",), acceptance_checks=("pytest",))
    assert task.request_text == task.objective == "Fix the lifecycle bug"
    assert TaskContract.from_request("Explain and fix the bug", origin="discord:1", owner="alice",
                                    request_revision="1", profile="default",
                                    repo_root="/tmp/work", workdir="/tmp/work",
                                    allowed_paths=("/tmp/work",), acceptance_checks=("pytest",)) is not None


@pytest.mark.parametrize("field,value", [
    ("request_revision", "2"), ("profile", "other"), ("repo_root", "/tmp/work/subrepo"),
])
def test_key_includes_each_request_identity_field(field, value):
    task = contract()
    changes = {field: value}
    if field == "repo_root":
        changes["workdir"] = value
    changed = replace(task, **changes)
    assert changed.idempotency_key() != task.idempotency_key()
    assert changed.digest() != task.digest()


def test_normalized_repo_identity(tmp_path):
    alias = tmp_path / "alias"
    repo = tmp_path / "repo"
    repo.mkdir()
    alias.symlink_to(repo, target_is_directory=True)
    task = contract(allowed_paths=(str(tmp_path),), repo_root=str(repo), workdir=str(repo))
    same = replace(task, repo_root=str(alias), workdir=str(alias))
    assert same.repo_root == str(repo.resolve())
    assert task.idempotency_key() == same.idempotency_key()
    assert task.digest() == same.digest()


@pytest.mark.parametrize("field", ["repo_root", "workdir"])
def test_scope_must_contain_repo_and_workdir(field):
    from agent.task_lifecycle.types import LifecycleError
    with pytest.raises(LifecycleError):
        contract(**{field: "/tmp/other"})


def test_workdir_must_be_inside_repo():
    from agent.task_lifecycle.types import LifecycleError
    with pytest.raises(LifecycleError):
        contract(allowed_paths=("/tmp",), repo_root="/tmp/repo", workdir="/tmp/other")
