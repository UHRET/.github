import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
spec = importlib.util.spec_from_file_location("sync_mirror", Path(__file__).with_name("sync-mirror.py"))
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


class ConfigurationTests(unittest.TestCase):
    def test_organization_and_automatic_mappings(self):
        self.assertEqual(mirror.ORG, "uhret")
        config = Path(__file__).resolve().parents[1] / "config" / "mirrors.json"
        entries = json.loads(config.read_text(encoding="utf-8"))
        self.assertCountEqual(
            [
                (entry["source"], entry["source_id"], entry["destination"],
                 entry["destination_id"], entry["mode"])
                for entry in entries
            ],
            [
                ("baban9999ytr/ARES-Mission-Control", 1457577185, "uhret/ARES-Mission-Control", 1473070854, "automatic"),
                ("grassified/EarthSona", 1457837796, "uhret/EarthSona", 1473069003, "automatic"),
                ("Hakanefe25/caly-full", 1458845714, "uhret/caly-full", 1473142325, "automatic"),
                ("Hakanefe25/caly-demo", 1458807544, "uhret/caly-demo", 1473142452, "automatic"),
                ("eeea2222/QK-norm-UHRET", 1458982774, "uhret/QK-norm-UHRET", 1473070873, "automatic"),
                ("ilyascinar593-ops/solar-pulse", 1458958701, "uhret/solar-pulse", 1473070859, "automatic"),
            ],
        )

    def test_rejects_other_destination_owners_before_network_access(self):
        for owner in ("former-owner", "uhret-other"):
            with self.subTest(owner=owner), patch.object(mirror, "verify_settings") as verify:
                entry = {"source": "fixture/Project", "source_id": 10,
                         "destination": f"{owner}/Project", "destination_id": 20}
                with self.assertRaisesRegex(mirror.SyncError, "Invalid source or destination"):
                    mirror.sync(entry)
                verify.assert_not_called()


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

    def test_destination_and_parent_ids(self):
        self.assertEqual(self.verify(), "refs/heads/main")
        self.destination["id"] = 21
        with self.assertRaisesRegex(mirror.SyncError, "destination repository identity"):
            self.verify()
        self.destination["id"] = 20
        self.destination["parent"] = self.source | {"id": 11}
        with self.assertRaisesRegex(mirror.SyncError, "native fork"):
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


class PublicApiTests(unittest.TestCase):
    def error(self, status, headers, body=b""):
        message = Message()
        for name, value in headers.items():
            message[name] = value
        return HTTPError("https://api.github.com/", status, "fixture", message, io.BytesIO(body))

    def test_primary_limit_waits_for_reset_and_retry_after(self):
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030", "Retry-After": "5"}
        with patch.object(mirror, "urlopen", side_effect=[self.error(403, headers), io.BytesIO(b"[]")]) as request, \
                patch.object(mirror.time, "time", return_value=1000), \
                patch.object(mirror.time, "sleep") as sleep, patch.object(mirror, "report"):
            self.assertEqual(mirror.public_api("repos/fixture/Project"), [])
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(31)

    def test_secondary_limit_defaults_to_one_minute_and_only_one_retry(self):
        body = b'{"message":"You have exceeded a secondary rate limit."}'
        errors = [self.error(403, {}, body), self.error(429, {}, body)]
        with patch.object(mirror, "urlopen", side_effect=errors) as request, \
                patch.object(mirror.time, "sleep") as sleep, patch.object(mirror, "report"):
            with self.assertRaisesRegex(mirror.SyncError, "persisted after one retry"):
                mirror.public_api("repos/fixture/Project")
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(60)

    def test_does_not_retry_access_failures_or_excessive_waits(self):
        cases = [
            (self.error(403, {}, b"Forbidden"), "check visibility and access"),
            (self.error(429, {"Retry-After": "121"}), "exceeds 120s"),
            (self.error(403, {"X-RateLimit-Remaining": "0"}), "retry window is unknown"),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected), patch.object(mirror, "urlopen", side_effect=error) as request, \
                    patch.object(mirror.time, "sleep") as sleep:
                with self.assertRaisesRegex(mirror.SyncError, expected):
                    mirror.public_api("repos/fixture/Project")
                request.assert_called_once()
                sleep.assert_not_called()


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
