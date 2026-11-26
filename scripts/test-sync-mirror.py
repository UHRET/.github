import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
spec = importlib.util.spec_from_file_location("sync_mirror", Path(__file__).with_name("sync-mirror.py"))
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.source = {"full_name": "fixture/Project", "id": 10, "visibility": "public", "default_branch": "main"}
        self.destination = {"full_name": f"{mirror.ORG}/Project", "id": 20, "visibility": "public",
                            "default_branch": "main", "parent": self.source,
                            "fork": True, "has_pages": False}
        self.entry = {"source": self.source["full_name"], "source_id": 10,
                      "destination": self.destination["full_name"], "destination_id": 20}

    def verify(self, actions=False, deployment=None):
        with patch.object(mirror, "public_api", side_effect=[self.source, deployment or []]), \
                patch.object(mirror, "private_api", side_effect=[self.destination, {"enabled": actions}]):
            return mirror.verify_settings(self.entry)

    def test_settings(self):
        self.assertEqual(self.verify(), "refs/heads/main")
        with self.assertRaisesRegex(mirror.SyncError, "Actions"):
            self.verify(actions=True)
        with self.assertRaisesRegex(mirror.SyncError, "deployment"):
            self.verify(deployment=[{"id": 3}])
        self.destination["has_pages"] = True
        with self.assertRaisesRegex(mirror.SyncError, "Pages"):
            self.verify()

    def test_identity_and_private_upstream(self):
        self.source["id"] = 99
        with self.assertRaisesRegex(mirror.SyncError, "identity"):
            self.verify()
        self.source["id"] = 10
        self.source["visibility"] = "private"
        with self.assertRaisesRegex(mirror.SyncError, "active public"):
            self.verify()

    def test_auth_failure_redacts_raw_output(self):
        failure = subprocess.CompletedProcess([], 1, b"", b"do not report credential material")
        with patch.object(mirror.subprocess, "run", return_value=failure):
            with self.assertRaises(mirror.SyncError) as raised:
                mirror.private_api("repos/fixture/repo")
        self.assertIn("GitHub App access", str(raised.exception))
        self.assertNotIn("credential material", str(raised.exception))

    def test_one_transient_retry(self):
        transient = subprocess.CompletedProcess([], 1, b"", b"connection reset")
        good = subprocess.CompletedProcess([], 0, b"ok", b"")
        with patch.object(mirror.time, "sleep"), patch.object(mirror, "git", side_effect=[transient, good]) as git:
            self.assertEqual(mirror.read_git(".", "Checking", "status"), b"ok")
            self.assertEqual(git.call_count, 2)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.up = self.root / "upstream.git"
        self.down = self.root / "fork.git"
        self.work = self.root / "work"
        for remote in (self.up, self.down):
            self.git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
        self.git(self.root, "init", "-q", "-b", "main", str(self.work))
        self.source = "fixture/Project"
        self.entry = {"source": self.source, "source_id": 10, "destination": f"{mirror.ORG}/Project",
                      "destination_id": 20, "mode": "automatic"}
        self.commit("README", "init")
        for remote in (self.up, self.down):
            self.git(self.work, "push", str(remote), "main")
        url = patch.object(mirror, "git_url", side_effect=lambda name: str(self.up if name == self.source else self.down))
        settings = patch.object(mirror, "verify_settings", return_value="refs/heads/main")
        for item in (url, settings):
            item.start()
            self.addCleanup(item.stop)

    def git(self, cwd, *args):
        return subprocess.check_output(
            ["git", "-C", str(cwd), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
            stderr=subprocess.STDOUT,
        ).decode().strip()

    def commit(self, name, text):
        (self.work / name).write_text(text)
        self.git(self.work, "add", name)
        self.git(self.work, "commit", "-qm", "fixture change")

    def sync(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return mirror.sync(self.entry, **kwargs)

    def test_noop_branch_tag_and_ordinary_updates(self):
        self.assertIn("Verified 1", self.sync())
        self.commit("README", "normal update")
        self.git(self.work, "branch", "topic/feature")
        self.git(self.work, "tag", "-a", "v1", "-m", "release marker")
        self.git(self.work, "push", str(self.up), "main", "topic/feature", "v1")
        self.assertIn("Dry run", self.sync(dry_run=True))
        self.assertNotEqual(mirror.remote_refs(self.root, str(self.up)), mirror.remote_refs(self.root, str(self.down)))
        self.assertIn("after push", self.sync())
        self.assertEqual(mirror.remote_refs(self.root, str(self.up)), mirror.remote_refs(self.root, str(self.down)))
        self.git(self.work, "push", str(self.up), ":topic/feature")
        self.assertIn("Verified 2", self.sync())
        self.assertIn("refs/heads/topic/feature", mirror.remote_refs(self.root, str(self.down)))

    def test_git_failures_do_not_affect_next_target(self):
        self.commit("README", "local changes")
        self.git(self.work, "push", str(self.down), "main")
        self.git(self.work, "reset", "--hard", "HEAD^")
        self.commit("README", "different upstream change")
        self.git(self.work, "push", str(self.up), "main")
        with self.assertRaisesRegex(mirror.SyncError, "Divergence"):
            self.sync()
        self.git(self.work, "push", "--force", str(self.down), "main")
        self.assertIn("Verified", self.sync())
        self.git(self.work, "reset", "--hard", "HEAD^")
        self.git(self.work, "push", "--force", str(self.up), "main")
        with self.assertRaisesRegex(mirror.SyncError, "rewrite"):
            self.sync()

    def test_replaced_tag(self):
        self.git(self.work, "tag", "-a", "v1", "-m", "one")
        for remote in (self.up, self.down):
            self.git(self.work, "push", str(remote), "v1")
        self.git(self.work, "tag", "-f", "-a", "v1", "-m", "two")
        self.git(self.work, "push", "--force", str(self.up), "v1")
        with self.assertRaisesRegex(mirror.SyncError, "replaced"):
            self.sync()

    def test_snapshot_and_sensitive_incoming_objects(self):
        self.entry["mode"] = "snapshot"
        self.entry["approved_refs"] = mirror.remote_refs(self.root, str(self.up))
        self.commit("change.txt", "gh" + "p_" + "A" * 36)
        self.git(self.work, "push", str(self.up), "main")
        with self.assertRaisesRegex(mirror.SyncError, "reviewed snapshot"):
            self.sync()
        self.entry["mode"] = "automatic"
        with self.assertRaisesRegex(mirror.SyncError, "Possible credential"):
            self.sync()
        self.git(self.work, "reset", "--hard", "HEAD^")
        self.commit("payload.bin", "version https://git-lfs.github.com/spec/v1\n"
                    "oid sha256:" + "b" * 64 + "\nsize 10\n")
        self.git(self.work, "push", "--force", str(self.up), "main")
        with self.assertRaisesRegex(mirror.SyncError, "Git LFS"):
            self.sync()

    def test_notice_changes_pause_the_target(self):
        self.commit("LICENSE", "first notice")
        self.git(self.work, "push", str(self.up), "main")
        self.assertIn("after push", self.sync())
        self.commit("LICENSE", "changed terms")
        self.git(self.work, "push", str(self.up), "main")
        with self.assertRaisesRegex(mirror.SyncError, "license"):
            self.sync()


if __name__ == "__main__":
    unittest.main(verbosity=2)
