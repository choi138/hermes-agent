#!/usr/bin/python3
"""Snapshot a git worktree and check subsequent writes against allowed scope."""

import argparse
import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import sys


GENERATED = ("*__pycache__/*", "__pycache__/*", "*.pyc", ".pytest_cache/*")
PROTECTED = ("node_modules/", ".env")


def git(repo, *args):
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", repo, *args], check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        lines = os.fsdecode(result.stderr).splitlines()
        raise ValueError(lines[0] if lines else "git command failed")
    return result.stdout


def paths(repo, *args):
    return {os.fsdecode(path) for path in git(repo, *args).split(b"\0") if path}


def root(directory):
    repo = os.path.realpath(directory)
    top = os.fsdecode(git(repo, "rev-parse", "--show-toplevel")).rstrip("\n")
    if top != repo:
        raise ValueError("--repo must be the root of a git worktree")
    return repo


def state(path):
    info = os.lstat(path)
    value = {"type": "other", "mode": info.st_mode & 0o7777,
             "target": None, "sha256": None}
    if stat.S_ISLNK(info.st_mode):
        value.update(type="symlink", target=os.readlink(path))
    elif stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        value.update(type="file", sha256=digest.hexdigest())
    elif stat.S_ISDIR(info.st_mode):
        value.update(type="dir")
    return value


def dir_added_or_removed(before, after):
    return ((before is None and after is not None and after["type"] == "dir")
            or (after is None and before is not None and before["type"] == "dir"))


def raise_error(error):
    raise error


def tree_paths(directory, skip_git=False):
    for parent, directories, files in os.walk(directory, onerror=raise_error, followlinks=False):
        if skip_git and parent == directory:
            directories[:] = [name for name in directories if name != ".git"]
            files = [name for name in files if name != ".git"]
        for name in sorted(directories + files):
            yield os.path.join(parent, name)


def files(repo):
    return {os.path.relpath(path, repo).replace(os.sep, "/"): state(path)
            for path in tree_paths(repo, skip_git=True)}


def git_state(repo, directories):
    common, worktree = directories
    locations = [os.path.join(common, "config"), os.path.join(common, "info", "attributes"),
                 os.path.join(common, "info", "exclude"),
                 os.path.join(common, "hooks")]
    if common != worktree:
        locations.extend(os.path.join(worktree, name)
                         for name in ("config.worktree", "commondir", "gitdir"))
    result = {}
    pointer = os.path.join(repo, ".git")
    if os.path.lexists(pointer):
        result[pointer] = state(pointer)
    for location in locations:
        if not os.path.lexists(location):
            continue
        entries = tree_paths(location) if stat.S_ISDIR(os.lstat(location).st_mode) else [location]
        for path in entries:
            mode = os.lstat(path).st_mode
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                result[os.path.realpath(path)] = state(path)
    return result


def index_flags(repo):
    return sorted(os.fsdecode(entry[2:])
                  for entry in git(repo, "ls-files", "-v", "-z").split(b"\0")
                  if entry and (entry[:1].islower() or entry[:1] == b"S"))


def report(changed, violations):
    for path in sorted(changed):
        print("changed=" + path)
    for path, reason in violations:
        print("violation={} reason={}".format(path, reason))
    print("scope=violation count={}".format(len(violations)) if violations else "scope=ok")
    return 1 if violations else 0


def snapshot(args, repo):
    output = os.path.realpath(args.out)
    if os.path.commonpath([repo, output]) == repo:
        raise ValueError("snapshot output must be outside the repo")
    base = os.fsdecode(git(repo, "rev-parse", "HEAD")).strip()
    directories = [os.path.realpath(os.path.join(repo, os.fsdecode(
        git(repo, "rev-parse", option)).rstrip("\n")))
        for option in ("--git-common-dir", "--git-dir")]
    data = {"repo": repo, "base": base, "files": files(repo),
            "git_dirs": directories, "git_state": git_state(repo, directories),
            "index_flags": index_flags(repo)}
    temporary = args.out + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        try:
            json.dump(data, stream, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
        except BaseException:
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, args.out)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    print("snapshot={} base={} files={}".format(args.out, base, len(data["files"])))
    return 0


def check(args, repo):
    with open(args.snapshot, encoding="utf-8") as stream:
        saved = json.load(stream)
    if saved["repo"] != repo:
        root(repo)
        raise ValueError("snapshot repo does not match --repo")
    controls = git_state(repo, saved["git_dirs"])
    changed_controls = {path for path in controls.keys() | saved["git_state"].keys()
                        if controls.get(path) != saved["git_state"].get(path)}
    if changed_controls:
        return report(changed_controls, [(path, "git_state") for path in sorted(changed_controls)])
    root(repo)
    flagged = set(index_flags(repo)) - set(saved["index_flags"])
    base = saved["base"]
    previous = saved["files"]
    current = files(repo)
    changed = {path for path in previous.keys() | current.keys()
               if previous.get(path) != current.get(path)
               and not dir_added_or_removed(previous.get(path), current.get(path))} | flagged
    changed.update(paths(repo, "diff", "--name-only", "--no-renames", "-z", base, "HEAD"))
    changed.update(paths(repo, "diff", "--name-only", "--no-renames", "-z", "--cached", base))
    tracked = paths(repo, "ls-tree", "-r", "--name-only", "-z", base)
    generated = changed - tracked
    existing = {path.casefold() for path in previous.keys() | tracked}
    violations = []
    for path in sorted(changed):
        folded = path.casefold()
        parts = folded.split("/")
        protected = ((folded in existing and any(part.startswith(".env") for part in parts))
                     or "node_modules" in parts[:-1]
                     or any(folded.startswith(prefix.casefold())
                            for prefix in PROTECTED + tuple(args.protect)))
        allowed = any(path == allow or (allow.endswith("/") and path.startswith(allow))
                      for allow in args.allow)
        allowed = allowed or (path in generated and any(
            fnmatch.fnmatchcase(path, pattern) for pattern in GENERATED + tuple(args.allow_generated)))
        if path in flagged:
            violations.append((path, "index_flag"))
        elif protected:
            violations.append((path, "protected"))
        elif not allowed:
            violations.append((path, "out_of_scope"))
    return report(changed, violations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="save HEAD and worktree content states")
    snap.add_argument("--repo", required=True, help="git worktree root")
    snap.add_argument("--out", required=True, help="snapshot JSON outside the worktree")
    verify = commands.add_parser("check", help="report changed paths and scope violations")
    verify.add_argument("--repo", required=True, help="git worktree root")
    verify.add_argument("--snapshot", required=True, help="pre-run snapshot JSON")
    verify.add_argument("--allow", action="append", default=[], help="allowed path or directory ending in /")
    verify.add_argument("--allow-generated", action="append", default=[], help="additional generated-file glob")
    verify.add_argument("--protect", action="append", default=[], help="additional protected prefix")
    args = parser.parse_args()
    try:
        repo = root(args.repo) if args.command == "snapshot" else os.path.realpath(args.repo)
        return snapshot(args, repo) if args.command == "snapshot" else check(args, repo)
    except (OSError, ValueError, KeyError, TypeError) as error:
        lines = str(error).splitlines()
        print(lines[0] if lines else "scope check failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
