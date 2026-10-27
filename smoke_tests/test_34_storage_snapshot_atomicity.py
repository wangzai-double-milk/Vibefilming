from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from film import storage


class _Output:
    def __init__(self, data: bytes):
        self.data = data

    def read(self):
        return self.data


class _FakeTos:
    def __init__(self):
        self.objects = {}
        self.fail_index = False

    def put_object_from_file(self, _bucket, key, path, **_kwargs):
        self.objects[key] = Path(path).read_bytes()

    def put_object(self, _bucket, key, content=None, **_kwargs):
        if self.fail_index and key.endswith("/.tos_sync.json"):
            raise RuntimeError("mock index publish failure")
        self.objects[key] = bytes(content or b"")

    def get_object(self, _bucket, key):
        if key not in self.objects:
            error = RuntimeError("missing")
            error.status_code = 404
            raise error
        return _Output(self.objects[key])

    def get_object_to_file(self, _bucket, key, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.objects[key])

    def pre_signed_url(self, _method, _bucket, key, **_kwargs):
        return SimpleNamespace(
            signed_url=f"https://tos.example/{key}?sig=test")


class StorageSnapshotAtomicityTest(unittest.TestCase):
    def test_failed_index_publish_keeps_last_complete_snapshot_restorable(self):
        fake = _FakeTos()
        environment = {
            "TOS_ACCESS_KEY": "ak",
            "TOS_SECRET_KEY": "sk",
            "TOS_BUCKET": "bucket",
            "TOS_PREFIX": "vf",
        }
        with TemporaryDirectory() as temp, patch.dict(
            os.environ, environment, clear=False
        ), patch.object(storage, "_clients", return_value=(fake, fake)):
            source = Path(temp) / "source"
            source.mkdir()
            manifest = source / "manifest.json"
            manifest.write_text(
                json.dumps({"version": "old"}), encoding="utf-8")
            first = storage.sync_directory(
                source, "projects/p1", session_id="session-a")
            self.assertTrue(first["revision"])

            manifest.write_text(
                json.dumps({"version": "new"}), encoding="utf-8")
            fake.fail_index = True
            with self.assertRaisesRegex(
                RuntimeError, "mock index publish failure"
            ):
                storage.sync_directory(
                    source, "projects/p1", session_id="session-a")

            fake.fail_index = False
            restored = Path(temp) / "restored"
            storage.restore_directory(
                "projects/p1", restored, session_id="session-a")
            self.assertEqual(
                {"version": "old"},
                json.loads(
                    (restored / "manifest.json").read_text(encoding="utf-8")),
            )
            index = json.loads(
                (restored / ".tos_sync.json").read_text(encoding="utf-8"))
            self.assertEqual(first["revision"], index["revision"])

    def test_large_same_size_same_mtime_change_gets_new_object(self):
        fake = _FakeTos()
        environment = {
            "TOS_ACCESS_KEY": "ak",
            "TOS_SECRET_KEY": "sk",
            "TOS_BUCKET": "bucket",
            "TOS_PREFIX": "vf",
        }
        with TemporaryDirectory() as temp, patch.dict(
            os.environ, environment, clear=False
        ), patch.object(storage, "_clients", return_value=(fake, fake)):
            source = Path(temp) / "source"
            source.mkdir()
            media = source / "large.mp4"
            media.write_bytes(b"a" * (9 * 1024 * 1024))
            original_mtime = media.stat().st_mtime_ns
            storage.sync_directory(
                source, "projects/p2", session_id="session-a")

            media.write_bytes(b"b" * (9 * 1024 * 1024))
            os.utime(
                media,
                ns=(media.stat().st_atime_ns, original_mtime),
            )
            second = storage.sync_directory(
                source, "projects/p2", session_id="session-a")
            self.assertEqual(1, second["uploaded"])

            restored = Path(temp) / "restored-large"
            storage.restore_directory(
                "projects/p2", restored, session_id="session-a")
            self.assertEqual(
                storage._fingerprint(media)["sha256"],
                storage._fingerprint(restored / "large.mp4")["sha256"],
            )


if __name__ == "__main__":
    unittest.main()
