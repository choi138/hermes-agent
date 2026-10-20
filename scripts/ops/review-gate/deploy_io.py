"""Host-local deployment engine, also sent verbatim to Python over SSH (3.9+)."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

MANIFEST = "~/.local/state/review-gate/deploy-manifest.json"


def digest(content):
    return hashlib.sha256(content).hexdigest()


def absolute(path):
    # Do not resolve the final component: a symlink must remain visible.
    return Path(os.path.abspath(os.path.expanduser(path)))


def inspect_targets(targets):
    result = []
    for target in targets:
        path = absolute(target["path"])
        if path.is_symlink():
            raise ValueError(f"Refusing symlink destination: {path}")
        content = base64.b64decode(target["content"], validate=True)
        current = path.read_bytes() if path.exists() else None
        mode = stat.S_IMODE(path.stat().st_mode) if current is not None else None
        sha = digest(content)
        status = "create" if current is None else (
            "unchanged" if current == content and mode == target["mode"] else "update")
        result.append(dict(id=target["id"], path=str(path), status=status,
                           sha256=sha, deployed=digest(current) if current is not None else None))
    return result


def preflight(targets):
    result = inspect_targets(targets)
    manifest = absolute(MANIFEST)
    if manifest.is_symlink():
        raise ValueError(f"Refusing symlink manifest: {manifest}")
    if manifest.exists() and not manifest.is_file():
        raise ValueError(f"Manifest is not a regular file: {manifest}")
    # Check all parent chains below HOME before writing any file on this host. System
    # ancestors such as macOS /var -> /private/var are outside the managed tree.
    home = absolute("~")
    for path in [absolute(t["path"]) for t in targets] + [manifest]:
        for parent in path.parents:
            if home not in parent.parents:
                continue
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError(f"Refusing destination parent: {parent}")
    return result


def make_parents(path):
    if path.is_dir():
        return
    make_parents(path.parent)
    path.mkdir(mode=0o700)


def atomic_write(path, content, mode):
    make_parents(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".review-gate-", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, str(path))
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def backup(path):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")  # noqa: UP017 -- Python 3.9 runtime
    destination = path.with_name(path.name + ".bak-" + stamp)
    # O_EXCL reserves this name, including against dangling symlinks.
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        shutil.copy2(str(path), str(destination))
    except OSError:
        destination.unlink()
        raise


def operate(payload):
    targets = payload["targets"]
    result = preflight(targets)
    if payload["action"] != "apply":
        return result
    # Refuse backup collisions before modifying anything on this host.
    for target, row in zip(targets, result):
        if row["deployed"] is not None and row["deployed"] != row["sha256"]:
            path = absolute(target["path"])
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")  # noqa: UP017 -- Python 3.9 runtime
            candidate = path.with_name(path.name + ".bak-" + stamp)
            if os.path.lexists(str(candidate)):
                raise ValueError(f"Backup already exists: {candidate}")
    for target, row in zip(targets, result):
        if row["status"] == "unchanged":
            continue
        path = absolute(target["path"])
        if row["deployed"] is not None and row["deployed"] != row["sha256"]:
            backup(path)
        atomic_write(path, base64.b64decode(target["content"]), target["mode"])
    manifest = dict(version=1, source_commit=payload["source_commit"],
                    deployed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),  # noqa: UP017 -- Python 3.9 runtime
                    host=payload["host"], targets=[dict(id=r["id"], path=r["path"],
                    sha256=r["sha256"], mode=format(t["mode"], "04o")) for t, r in zip(targets, result)])
    manifest_path = absolute(MANIFEST)
    make_parents(manifest_path.parent)
    manifest_path.parent.chmod(0o700)
    atomic_write(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"), 0o600)
    return result
