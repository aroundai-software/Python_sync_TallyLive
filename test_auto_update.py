"""Automated checks for auto-update versioning, zip layout, hash, and extract/preserve."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.auto_update import _sha256_file, check_and_apply_update, is_newer
from src.version import APP_VERSION
from updater import updater_main


ROOT = Path(__file__).resolve().parent
ZIP_1_1_1 = ROOT / "installer_output" / "Tallylive_python_sync_background_1.1.1.zip"
SHA_FILE = ROOT / "installer_output" / "Tallylive_python_sync_background_1.1.1.zip.sha256.txt"


class VersionCompareTests(unittest.TestCase):
    def test_newer_detection(self):
        self.assertTrue(is_newer("1.1.1", "1.1.0"))
        self.assertTrue(is_newer("1.2.0", "1.1.9"))
        self.assertFalse(is_newer("1.1.1", "1.1.1"))
        self.assertFalse(is_newer("1.1.0", "1.1.1"))
        self.assertTrue(is_newer("2.0.0", "1.9.9"))


class BuiltZipTests(unittest.TestCase):
    def test_zip_exists_and_named_with_version(self):
        self.assertTrue(ZIP_1_1_1.is_file(), f"Missing {ZIP_1_1_1}")
        self.assertIn("1.1.1", ZIP_1_1_1.name)

    def test_sha256_matches_file(self):
        self.assertTrue(SHA_FILE.is_file())
        meta = SHA_FILE.read_text(encoding="utf-8").strip().splitlines()
        expected = meta[0].strip().lower()
        actual = _sha256_file(str(ZIP_1_1_1))
        self.assertEqual(actual, expected)
        joined = "\n".join(meta)
        self.assertIn("storage_path=1.1.1/Tallylive_python_sync_background_1.1.1.zip", joined)

    def test_zip_contains_exe_under_sync_folder(self):
        with zipfile.ZipFile(ZIP_1_1_1) as zf:
            names = zf.namelist()
        self.assertTrue(
            any(n.endswith("Tallylive_python_sync_background.exe") for n in names)
        )
        self.assertTrue(
            any(n.startswith("Tallylive_python_sync_background/") for n in names)
        )
        # Must not ship client secrets / runtime DB in update zip
        lowered = [n.lower() for n in names]
        self.assertFalse(any(n.endswith(".env") for n in lowered))
        self.assertFalse(any("sync_cache.db" in n for n in lowered))


class ExtractPreserveTests(unittest.TestCase):
    def test_extract_preserves_env_and_data(self):
        td = tempfile.mkdtemp(prefix="tl_upd_test_")
        try:
            install_root = Path(td) / "TallyLive Sync"
            target = install_root / "Tallylive_python_sync_background"
            target.mkdir(parents=True)
            (target / ".env").write_text("TALLY_HOST=keepme\nTALLY_PORT=9000\n", encoding="utf-8")
            data = target / "data"
            data.mkdir()
            (data / "sync_cache.db").write_bytes(b"sqlite-bytes")
            (target / "old_file.txt").write_text("remove-me", encoding="utf-8")
            (target / "Tallylive_python_sync_background.exe").write_bytes(b"old-exe")

            log_path = str(install_root / "logs" / "update.log")
            updater_main.extract_update(str(ZIP_1_1_1), str(target), log_path)

            self.assertTrue(updater_main.health_ok(str(target)))
            self.assertTrue((target / "Tallylive_python_sync_background.exe").is_file())
            self.assertGreater((target / "Tallylive_python_sync_background.exe").stat().st_size, 1000)
            self.assertEqual(
                (target / ".env").read_text(encoding="utf-8"),
                "TALLY_HOST=keepme\nTALLY_PORT=9000\n",
            )
            self.assertEqual((data / "sync_cache.db").read_bytes(), b"sqlite-bytes")
            self.assertFalse((target / "old_file.txt").exists())
            self.assertTrue((target / "_internal").is_dir())
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_bad_hash_aborts_before_apply(self):
        """Simulate download hash mismatch — check_and_apply_update returns False."""
        fake_release = {
            "version": "9.9.9",
            "storage_path": "9.9.9/x.zip",
            "sha256": "0" * 64,
            "is_active": True,
        }
        zip_bytes = ZIP_1_1_1.read_bytes()

        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
            fake_release
        ]
        mock_sb.storage.from_.return_value.download.return_value = zip_bytes

        with patch("src.auto_update.load_config", return_value={
            "supabase_url": "https://example.supabase.co",
            "supabase_key": "x",
        }), patch("supabase.create_client", return_value=mock_sb):
            # Force local version older so it tries download
            with patch("src.auto_update.APP_VERSION", "1.0.0"):
                ok = check_and_apply_update()
        self.assertFalse(ok)


class MockHappyPathStageTests(unittest.TestCase):
    def test_stages_pending_and_launches_updater(self):
        zip_bytes = ZIP_1_1_1.read_bytes()
        actual_hash = hashlib.sha256(zip_bytes).hexdigest()
        fake_release = {
            "version": "9.9.9",
            "storage_path": "9.9.9/Tallylive_python_sync_background_9.9.9.zip",
            "sha256": actual_hash,
            "is_active": True,
        }

        td = tempfile.mkdtemp(prefix="tl_stage_")
        try:
            install_root = Path(td)
            app_dir = install_root / "Tallylive_python_sync_background"
            app_dir.mkdir()
            updater_exe = install_root / "Tallylive_updater.exe"
            updater_exe.write_bytes(b"MZ-fake")

            mock_sb = MagicMock()
            mock_sb.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
                fake_release
            ]
            mock_sb.storage.from_.return_value.download.return_value = zip_bytes

            pops = []

            def fake_popen(args, **kwargs):
                pops.append(args)
                return MagicMock()

            with patch("src.auto_update.load_config", return_value={
                "supabase_url": "https://example.supabase.co",
                "supabase_key": "x",
            }), patch("supabase.create_client", return_value=mock_sb), patch(
                "src.auto_update.get_app_dir", return_value=str(app_dir)
            ), patch(
                "src.auto_update.get_install_root", return_value=str(install_root)
            ), patch(
                "src.auto_update.get_updater_path", return_value=str(updater_exe)
            ), patch(
                "src.auto_update.APP_VERSION", "1.0.0"
            ), patch(
                "subprocess.Popen", side_effect=fake_popen
            ):
                ok = check_and_apply_update()

            self.assertTrue(ok)
            pending = install_root / "update_pending.json"
            self.assertTrue(pending.is_file())
            data = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], "9.9.9")
            self.assertEqual(data["from_version"], "1.0.0")
            self.assertTrue(zipfile.is_zipfile(data["zip_path"]))
            self.assertEqual(data["sha256"], actual_hash)
            self.assertEqual(len(pops), 1)
            self.assertEqual(pops[0][0], str(updater_exe))
        finally:
            shutil.rmtree(td, ignore_errors=True)


class LiveSupabaseChannelTests(unittest.TestCase):
    """Optional live checks — skip if table/bucket empty or unreachable."""

    def test_can_query_releases_table(self):
        from src.config import load_config
        from supabase import create_client

        cfg = load_config()
        sb = create_client(cfg["supabase_url"], cfg["supabase_key"])
        try:
            res = (
                sb.table("sync_app_releases")
                .select("version,storage_path,sha256,is_active")
                .eq("is_active", True)
                .limit(5)
                .execute()
            )
        except Exception as e:
            self.skipTest(f"Cannot query sync_app_releases: {e}")

        rows = res.data or []
        print(f"\n[Live] active releases: {rows}")
        if not rows:
            self.skipTest("No active release row yet — upload zip + activate in Supabase")
        row = rows[0]
        self.assertTrue(row.get("version"))
        self.assertTrue(row.get("storage_path"))
        self.assertTrue(row.get("sha256"))

        # Try download + hash if active row points at a real object
        try:
            raw = sb.storage.from_("sync-releases").download(row["storage_path"])
        except Exception as e:
            self.fail(f"Active release storage download failed: {e}")
        self.assertTrue(raw)
        h = hashlib.sha256(raw).hexdigest()
        self.assertEqual(
            h,
            str(row["sha256"]).lower(),
            "SHA256 in table must match downloaded zip",
        )
        self.assertEqual(raw[:2], b"PK", "Downloaded object is not a zip")
        import io
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(raw)))


if __name__ == "__main__":
    print(f"Local APP_VERSION={APP_VERSION}")
    unittest.main(verbosity=2)
