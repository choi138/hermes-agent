"""Exercise scope checks in isolated git worktrees using the system Python."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scope_check.py"
PYTHON = "/usr/bin/python3"


class ScopeCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix="scope-check-tests-"))
        self.addCleanup(shutil.rmtree, self.temporary)
        self.repo = self.temporary / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "scope@example.invalid")
        self.git("config", "user.name", "Scope Tests")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.temporary / "no-hooks"))
        self.write("in.py", "inside\n")
        self.write("out.py", "outside\n")
        self.write(".gitignore", ".env.local\nnode_modules/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.write(".env.local", "secret=original\n")
        self.write("node_modules/a/cli.js", "same content\n")
        self.write("node_modules/b/cli.js", "same content\n")
        self.write("node_modules/tool.sh", "echo tool\n")
        (self.repo / "node_modules/tool.sh").chmod(0o644)
        (self.repo / "node_modules/.bin").mkdir()
        (self.repo / "node_modules/.bin/check").symlink_to("../a/cli.js")
        self.write("notes/todo.md", "original note\n")
        self.snapshot = self.temporary / "snapshot.json"
        self.take_snapshot()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args],
                                check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def write(self, path, content):
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([PYTHON, str(SCRIPT), *map(str, args)],
                              check=False, capture_output=True, text=True)

    def take_snapshot(self):
        result = self.cli("snapshot", "--repo", self.repo, "--out", self.snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def linked_worktree(self):
        linked = self.temporary / "linked"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked))
        self.repo = linked
        self.take_snapshot()

    def check(self, expected, code, *extra, reasons=None):
        result = self.cli("check", "--repo", self.repo, "--snapshot", self.snapshot,
                          "--allow", "in.py", *extra)
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        lines = result.stdout.splitlines()
        changed = [line[len("changed="):] for line in lines if line.startswith("changed=")]
        self.assertEqual(set(changed), set(expected))
        self.assertEqual(changed, sorted(expected))
        violations = [line for line in lines if line.startswith("violation=")]
        self.assertEqual(violations, sorted(violations))
        self.assertEqual(lines, ["changed=" + path for path in sorted(expected)]
                         + violations + [lines[-1]])
        self.assertEqual(lines[-1], "scope=ok" if code == 0 else
                         "scope=violation count={}".format(len(violations)))
        if reasons is not None:
            self.assertEqual(violations, ["violation={} reason={}".format(path, reason)
                                          for path, reason in sorted(reasons.items())])
        return result

    def test_01_staged_edit_worktree_restored(self):
        self.write("out.py", "staged\n")
        self.git("add", "out.py")
        self.git("checkout", "--", "out.py")
        self.write("out.py", self.git("show", self.base + ":out.py").stdout)
        self.assertEqual(self.git("diff", "--name-only", self.base).stdout, "")
        self.check({"out.py"}, 1)

    def test_02_committed_rename(self):
        self.git("mv", "out.py", "new_in.py")
        self.git("commit", "-qm", "rename")
        self.check({"new_in.py", "out.py"}, 1)

    def test_03_ignored_env_edit(self):
        self.write(".env.local", "secret=changed\n")
        self.check({".env.local"}, 1, reasons={".env.local": "protected"})

    def test_04_commit_then_reset_to_base(self):
        self.write("out.py", "committed\n")
        self.git("add", "out.py")
        self.git("commit", "-qm", "edit")
        self.git("reset", "--soft", self.base)
        self.git("reset")
        self.git("checkout", "--", ".")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.check(set(), 0)

    def test_04b_committed_edit(self):
        self.write("out.py", "committed\n")
        self.git("add", "out.py")
        self.git("commit", "-qm", "edit")
        self.check({"out.py"}, 1)

    def test_05_new_untracked_file(self):
        self.write("stray.txt", "stray\n")
        self.check({"stray.txt"}, 1)

    def test_06_no_change(self):
        result = self.check(set(), 0)
        self.assertEqual(result.stdout, "scope=ok\n")

    def test_07_deleted_untracked_file(self):
        (self.repo / "notes/todo.md").unlink()
        self.check({"notes/todo.md"}, 1)

    def test_08_allowed_edit(self):
        self.write("in.py", "allowed\n")
        self.check({"in.py"}, 0)

    def test_09_existing_untracked_edit(self):
        self.write("notes/todo.md", "changed note\n")
        self.check({"notes/todo.md"}, 1)

    def test_10_retarget_symlink(self):
        link = self.repo / "node_modules/.bin/check"
        link.unlink()
        link.symlink_to("../b/cli.js")
        self.check({"node_modules/.bin/check"}, 1,
                   reasons={"node_modules/.bin/check": "protected"})

    def test_11_mode_change(self):
        (self.repo / "node_modules/tool.sh").chmod(0o755)
        self.check({"node_modules/tool.sh"}, 1)

    def test_12_symlink_replaced_with_file(self):
        (self.repo / "node_modules/.bin/check").unlink()
        self.write("node_modules/.bin/check", "same content\n")
        self.check({"node_modules/.bin/check"}, 1)

    def test_13_generated_bytecode(self):
        path = "pkg/__pycache__/x.cpython-39.pyc"
        self.write(path, "bytecode")
        self.check({path}, 0)

    def test_14_allow_directory(self):
        self.write("docs/a.md", "doc\n")
        self.check({"docs/a.md"}, 0, "--allow", "docs/")

    def test_directory_add_remove_ignored_but_mode_change_detected(self):
        (self.repo / "empty").mkdir()
        (self.repo / "notes").chmod(0o700)
        self.check({"notes"}, 1, reasons={"notes": "out_of_scope"})

    def test_unlistable_new_directory_fails_closed(self):
        hidden = self.repo / "packages" / "x" / "node_modules"
        hidden.mkdir(parents=True)
        (hidden / "a.js").write_text("evil\n", encoding="utf-8")
        hidden.chmod(0o311)
        try:
            result = self.cli("check", "--repo", self.repo, "--snapshot", self.snapshot,
                              "--allow", "packages/")
        finally:
            hidden.chmod(0o755)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_preexisting_generated_files_rewritten_ok(self):
        (self.repo / "pkg" / "__pycache__").mkdir(parents=True)
        pyc = "pkg/__pycache__/mod.cpython-39.pyc"
        self.write(pyc, "old")
        self.write(".pytest_cache/v/cache/lastfailed", "{}")
        self.take_snapshot()
        self.write(pyc, "new")
        (self.repo / ".pytest_cache/v/cache/lastfailed").unlink()
        self.check({".pytest_cache/v/cache/lastfailed", pyc}, 0, reasons={})

    def test_15_protected_overrides_allow(self):
        self.write(".env.local", "changed\n")
        self.check({".env.local"}, 1, "--allow", ".env.local",
                   reasons={".env.local": "protected"})

    def test_16_usage_errors(self):
        results = [self.cli("snapshot", "--repo", self.repo, "--out", self.repo / "bad.json"),
                   self.cli("snapshot", "--repo", self.repo / "notes", "--out", self.snapshot)]
        other = self.temporary / "other"
        other.mkdir()
        init = subprocess.run(["git", "-C", str(other), "init", "-q"],
                              check=False, capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr)
        results.append(self.cli("check", "--repo", other, "--snapshot", self.snapshot))
        for result in results:
            with self.subTest(stderr=result.stderr):
                self.assertEqual(result.returncode, 2)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertEqual(result.stdout, "")
        self.assertFalse((self.repo / "bad.json").exists())

    def test_snapshot_schema_and_atomic_success(self):
        result = self.take_snapshot()
        data = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"repo", "base", "files", "git_dirs",
                                     "git_state", "index_flags"})
        self.assertEqual(data["repo"], str(self.repo.resolve()))
        self.assertEqual(data["base"], self.base)
        self.assertEqual(set(data["files"]), {".gitignore", "in.py", "out.py",
                         ".env.local", "notes", "notes/todo.md", "node_modules",
                         "node_modules/a", "node_modules/b", "node_modules/.bin",
                         "node_modules/a/cli.js", "node_modules/b/cli.js",
                         "node_modules/tool.sh", "node_modules/.bin/check"})
        self.assertEqual(data["files"]["node_modules/.bin/check"],
                         {"type": "symlink", "mode": os.lstat(
                             self.repo / "node_modules/.bin/check").st_mode & 0o7777,
                          "target": "../a/cli.js", "sha256": None})
        self.assertEqual(data["files"]["node_modules/tool.sh"],
                         {"type": "file", "mode": 0o644, "target": None,
                          "sha256": hashlib.sha256(b"echo tool\n").hexdigest()})
        self.assertEqual(data["files"]["notes"],
                         {"type": "dir", "mode": (self.repo / "notes").stat().st_mode & 0o7777,
                          "target": None, "sha256": None})
        self.assertEqual(data["git_state"][str(self.repo.resolve() / ".git")]["type"], "dir")
        self.assertEqual(result.stdout, "snapshot={} base={} files=14\n".format(
            self.snapshot, self.base))
        self.assertFalse(Path(str(self.snapshot) + ".tmp").exists())

    def test_git_failure(self):
        directory = self.temporary / "not-git"
        directory.mkdir()
        expected = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                                  check=False, capture_output=True, text=True)
        for command, option, path in (("snapshot", "--out", self.temporary / "bad.json"),
                                      ("check", "--snapshot", self.snapshot)):
            result = self.cli(command, "--repo", directory, option, path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, expected.stderr.splitlines()[0] + "\n")

    def test_repeat_check_identical(self):
        self.write("stray.txt", "new\n")
        self.write("out.py", "changed\n")
        first = self.check({"stray.txt", "out.py"}, 1)
        second = self.check({"stray.txt", "out.py"}, 1)
        self.assertEqual(first.stdout, second.stdout)

    def test_committed_edit_with_index_and_worktree_restored(self):
        self.write("out.py", "committed\n")
        self.git("add", "out.py")
        self.git("commit", "-qm", "edit")
        self.git("checkout", self.base, "--", ".")
        self.assertEqual(self.git("diff", "--name-only", self.base).stdout, "")
        self.assertEqual(self.git("diff", "--cached", "--name-only", self.base).stdout, "")
        self.check({"out.py"}, 1)

    def test_dangling_symlink_present(self):
        link = self.repo / "dangling"
        link.symlink_to("missing")
        self.take_snapshot()
        self.check(set(), 0)
        link.unlink()
        self.check({"dangling"}, 1)

    def test_additional_protection_and_generated_globs(self):
        self.write("build/output.bin", "generated")
        self.write("private/a.txt", "protected")
        self.write("node_modules/new.pyc", "protected")
        self.check({"build/output.bin", "private/a.txt", "node_modules/new.pyc"}, 1,
                   "--allow-generated", "build/*", "--protect", "private/",
                   "--allow", "private/", "--allow", "node_modules/",
                   reasons={"private/a.txt": "protected", "node_modules/new.pyc": "protected"})

    def test_generated_glob_does_not_allow_base_tracked_file(self):
        self.check(set(), 0)
        self.write("out.py", "changed")
        self.check({"out.py"}, 1, "--allow-generated", "*.py",
                   reasons={"out.py": "out_of_scope"})
        (self.repo / "out.py").unlink()
        self.git("rm", "--cached", "out.py")
        self.write("out.py", "now untracked")
        self.check({"out.py"}, 1, "--allow-generated", "*.py")

    def test_snapshot_output_symlink_into_repo_rejected(self):
        link = self.temporary / "inside.json"
        link.symlink_to(self.repo / "bad.json")
        result = self.cli("snapshot", "--repo", self.repo, "--out", link)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.repo / "bad.json").exists())

    def test_unreadable_hash_file(self):
        path = self.repo / "notes/todo.md"
        path.chmod(0)
        try:
            for command, option, output in (("snapshot", "--out", self.snapshot),
                                             ("check", "--snapshot", self.snapshot)):
                result = self.cli(command, "--repo", self.repo, option, output)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn("Permission denied", result.stderr)
        finally:
            path.chmod(0o644)

    def test_nested_env_and_node_modules_are_protected(self):
        self.write("packages/x/node_modules/lib.js", "original\n")
        self.write("app/.env.local", "secret=original\n")
        self.take_snapshot()
        self.write("packages/x/node_modules/lib.js", "changed\n")
        self.write("app/.env.local", "secret=changed\n")
        self.check({"app/.env.local", "packages/x/node_modules/lib.js"}, 1, "--allow", "packages/",
                   "--allow", "app/", reasons={"app/.env.local": "protected",
                                               "packages/x/node_modules/lib.js": "protected"})

    def test_assume_unchanged_hides_nothing(self):
        self.write("out.py", "changed\n")
        self.git("update-index", "--assume-unchanged", "out.py")
        self.check({"out.py"}, 1, reasons={"out.py": "index_flag"})

    def test_skip_worktree_hides_nothing(self):
        self.write("out.py", "changed\n")
        self.git("update-index", "--skip-worktree", "out.py")
        self.check({"out.py"}, 1, reasons={"out.py": "index_flag"})

    def test_git_config_change_is_violation(self):
        self.git("config", "core.fsmonitor", "true")
        path = str((self.repo / ".git/config").resolve())
        self.check({path}, 1, reasons={path: "git_state"})
        environment = dict(os.environ, PATH=str(self.temporary / "no-git"))
        result = subprocess.run([PYTHON, str(SCRIPT), "check", "--repo", str(self.repo),
                                 "--snapshot", str(self.snapshot)], check=False,
                                capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertEqual(result.stdout.splitlines(), ["changed=" + path,
                         "violation=" + path + " reason=git_state", "scope=violation count=1"])

    def test_info_attributes_change_is_violation(self):
        self.write(".git/info/attributes", "*.py -diff\n")
        path = str((self.repo / ".git/info/attributes").resolve())
        self.check({path}, 1, reasons={path: "git_state"})

    def test_nested_repo_edit_detected(self):
        self.write(".gitignore", ".env.local\nnode_modules/\n.worktrees/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore nested worktrees")
        nested = self.repo / ".worktrees/x"
        nested.mkdir(parents=True)
        self.git("-C", str(nested), "init", "-q")
        self.write(".worktrees/x/a.py", "original\n")
        self.git("-C", str(nested), "add", "a.py")
        self.git("-C", str(nested), "-c", "user.name=Scope Tests", "-c",
                 "user.email=scope@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=" + str(self.temporary / "no-hooks"),
                 "commit", "-qm", "nested fixture")
        self.take_snapshot()
        data = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertNotIn(".worktrees/x/", data["files"])
        self.assertIn(".worktrees/x/.git", data["files"])
        self.assertIn(".worktrees/x/.git/config", data["files"])
        self.check(set(), 0)
        self.write(".worktrees/x/a.py", "changed\n")
        self.check({".worktrees/x/a.py"}, 1,
                   reasons={".worktrees/x/a.py": "out_of_scope"})

    def test_case_rename_env_protected(self):
        self.write("app/.env.local", "secret=original\n")
        self.take_snapshot()
        (self.repo / "app/.env.local").rename(self.repo / "app/.ENV.local")
        self.write("app/.ENV.local", "secret=changed\n")
        result = self.cli("check", "--repo", self.repo, "--snapshot", self.snapshot,
                          "--allow", "app/")
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertTrue(any(line.startswith("violation=") and line.endswith(" reason=protected")
                            for line in result.stdout.splitlines()), result.stdout)

    def test_case_rename_node_modules_protected(self):
        self.write("packages/x/node_modules/lib.js", "original\n")
        self.take_snapshot()
        (self.repo / "packages/x/node_modules").rename(self.repo / "packages/x/Node_Modules")
        self.write("packages/x/Node_Modules/lib.js", "changed\n")
        result = self.cli("check", "--repo", self.repo, "--snapshot", self.snapshot,
                          "--allow", "packages/")
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertTrue(any(line.startswith("violation=") and line.endswith(" reason=protected")
                            for line in result.stdout.splitlines()), result.stdout)

    def test_tracked_nested_env_protected(self):
        path = "apps/web/.env.production"
        self.write(path, "secret=original\n")
        self.git("add", "-f", path)
        self.git("commit", "-qm", "tracked nested env")
        self.take_snapshot()
        self.write(path, "secret=changed\n")
        self.check({path}, 1, "--allow", "apps/web/", reasons={path: "protected"})

    def test_new_nested_env_in_allowed_dir_ok(self):
        self.write("app/.env.example", "secret=example\n")
        self.check({"app/.env.example"}, 0, "--allow", "app/", reasons={})

    def test_preexisting_assume_unchanged_edit_detected(self):
        path = "config/local.yml"
        self.write(path, "local=original\n")
        self.git("add", path)
        self.git("commit", "-qm", "local config")
        self.git("update-index", "--assume-unchanged", path)
        self.take_snapshot()
        self.write(path, "local=changed\n")
        self.check({path}, 1, reasons={path: "out_of_scope"})

    def test_preexisting_skip_worktree_edit_detected(self):
        path = "config/local.yml"
        self.write(path, "local=original\n")
        self.git("add", path)
        self.git("commit", "-qm", "local config")
        self.git("update-index", "--skip-worktree", path)
        self.take_snapshot()
        self.write(path, "local=changed\n")
        self.check({path}, 1, reasons={path: "out_of_scope"})

    def test_commit_then_gc_is_ok(self):
        self.write("in.py", "committed\n")
        self.git("add", "in.py")
        self.git("commit", "-qm", "allowed edit")
        self.git("gc", "-q")
        self.check({"in.py"}, 0)

    def test_linked_worktree_gitfile_repoint_is_violation(self):
        self.linked_worktree()
        alternate = self.temporary / "alternate"
        alternate.mkdir()
        self.write(".git", "gitdir: {}\n".format(alternate))
        path = str(self.repo.resolve() / ".git")
        self.check({path}, 1, reasons={path: "git_state"})

    def test_linked_worktree_commondir_change_is_violation(self):
        self.linked_worktree()
        directory = self.git("rev-parse", "--git-dir").stdout.strip()
        path = (self.repo / directory / "commondir").resolve()
        path.write_text("../changed\n", encoding="utf-8")
        self.check({str(path)}, 1, reasons={str(path): "git_state"})

    def test_linked_worktree_honest_edit_ok(self):
        self.linked_worktree()
        self.write("in.py", "allowed\n")
        self.check({"in.py"}, 0)

    def test_help(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("snapshot", result.stdout)
        self.assertIn("check", result.stdout)


if __name__ == "__main__":
    unittest.main()
