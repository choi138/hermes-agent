"""Immutable, deterministic intake contracts; no model calls."""

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
import json
import re

from .types import LifecycleError


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def resolved_path(value):
    """Normalize filesystem identity without accepting traversal or NUL."""
    if not isinstance(value, (str, Path)) or "\0" in str(value):
        raise LifecycleError("Path must be absolute text without NUL")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise LifecycleError("Path must be absolute without '..'")
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise LifecycleError("Cannot resolve path") from exc


def _identity(name, value):
    if (not isinstance(value, str) or not value or not value.isprintable()
            or any(c.isspace() for c in value)):
        raise LifecycleError(f"{name} must be nonempty identity text without whitespace")


def _binding(value):
    for name in ("origin", "owner", "request_revision", "profile"):
        _identity(name, getattr(value, name))
    if not isinstance(value.allowed_paths, tuple) or not value.allowed_paths:
        raise LifecycleError("allowed_paths must be a nonempty tuple")
    allowed = tuple(str(resolved_path(p)) for p in value.allowed_paths)
    repo, workdir = resolved_path(value.repo_root), resolved_path(value.workdir)
    if any(not any(p.is_relative_to(Path(root)) for root in allowed) for p in (repo, workdir)):
        raise LifecycleError("repo_root and workdir must be inside allowed_paths")
    if not workdir.is_relative_to(repo):
        raise LifecycleError("workdir must be inside repo_root")
    if type(value.requires_approval) is not bool:
        raise LifecycleError("requires_approval must be a bool")
    if value.requires_approval:
        _identity("approval_ref", value.approval_ref)
    elif value.approval_ref is not None:
        raise LifecycleError("approval_ref is only valid when approval is required")
    object.__setattr__(value, "allowed_paths", allowed)
    object.__setattr__(value, "repo_root", str(repo))
    object.__setattr__(value, "workdir", str(workdir))


@dataclass(frozen=True)
class ExecutionAuthority:
    """Trusted caller's binding, supplied separately from request/model text.

    The caller must obtain this from authenticated intake and approval state.
    This draft checks equality; it does not authenticate an approval service.
    An approval reference cannot expand paths or override any other binding.
    """
    owner: str
    origin: str
    request_revision: str
    profile: str
    repo_root: str
    workdir: str
    allowed_paths: tuple[str, ...]
    requires_approval: bool = False
    approval_ref: str | None = None

    def __post_init__(self):
        _binding(self)
        # This private local identity is not model text or serialized ledger
        # data. Trusted intake creates authority while authorizing these objects.
        # Keeping no FDs here makes authority construction/replacement leak-free.
        from .directory_handoff import open_directory
        with open_directory(self.workdir) as (_, identities):
            object.__setattr__(self, "_directory_identity", identities)


@dataclass(frozen=True)
class TaskContract:
    request_text: str
    objective: str
    allowed_paths: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    requires_approval: bool
    origin: str
    owner: str
    request_revision: str = None
    profile: str = None
    repo_root: str = None
    workdir: str = None
    approval_ref: str | None = None
    execution_digest: str | None = None
    base_revision: str | None = None

    def __post_init__(self):
        for name in ("request_text", "objective", "origin", "owner"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise LifecycleError(f"{name} must be nonempty text")
        for name in ("allowed_paths", "forbidden_actions", "acceptance_checks"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise LifecycleError(f"{name} must be a tuple of nonempty strings")
        if not self.acceptance_checks:
            raise LifecycleError("acceptance_checks must not be empty")
        for name in ("execution_digest", "base_revision"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40,64}", value)):
                raise LifecycleError(f"Invalid {name}")
        _binding(self)

    def validate(self):
        # Catch changed symlinks as well as malformed/tampered instances before
        # reservation. Never silently rebind an already constructed contract.
        if TaskContract(**asdict(self)) != self:
            raise LifecycleError("Contract paths changed after validation")

    def bind(self, authority, request):
        self.validate()
        if not isinstance(authority, ExecutionAuthority):
            raise LifecycleError("A separate trusted ExecutionAuthority is required")
        if ExecutionAuthority(**asdict(authority)) != authority:
            raise LifecycleError("Authority paths changed after validation")
        for name, expected in asdict(authority).items():
            if getattr(self, name) != expected:
                raise LifecycleError(f"Contract authority mismatch: {name}")
        if (str(resolved_path(request.workdir)) != self.workdir
                or str(resolved_path(request.allowed_root)) != self.repo_root):
            raise LifecycleError("Request workdir/allowed_root does not match contract")

    def digest(self) -> str:
        values = asdict(self)
        for key in ("execution_digest", "base_revision"):
            if values[key] is None:
                values.pop(key)
        return _digest(values)

    def idempotency_key(self) -> str:
        return _digest({name: getattr(self, name) for name in
                        ("origin", "request_revision", "profile", "repo_root")})

    @classmethod
    def from_request(cls, text, *, origin, owner, objective=None, allowed_paths=(),
                     forbidden_actions=(), acceptance_checks=(), requires_approval=False,
                     request_revision=None, profile=None, repo_root=None, workdir=None,
                     approval_ref=None):
        """Bypass short informational requests, unless they also ask for action.

        Canonical JSON ignores serialization whitespace, not whitespace inside
        contract strings: changing the contract's actual content changes its hash.
        """
        if not isinstance(text, str) or not text.strip() or "\0" in text:
            raise LifecycleError("Request must be nonempty text without NUL")
        action = re.search(r"\b(fix|implement|create|change|delete|run|build|update|install)\b|수정|구현|삭제|실행|만들|추가", text, re.I)
        informational = re.search(r"^(what|why|how|explain|describe|show|list)\b|설명|알려|조회|상태|무엇", text.strip(), re.I)
        if len(text) <= 160 and informational and not action:
            return None
        return cls(text, text if objective is None else objective, allowed_paths,
                   forbidden_actions, acceptance_checks, requires_approval, origin, owner,
                   request_revision, profile, repo_root, workdir, approval_ref)
