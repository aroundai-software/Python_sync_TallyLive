"""
Clean build + package:
  1) PyInstaller: sync (onedir), settings (onefile), updater (onefile)
  2) dist_deployment/ layout for legacy zip + Inno
  3) Update zip for Supabase auto-update
  4) Compile Inno Setup → installer_output/Setup_TallyLive_Sync_*.exe
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

from src.version import APP_VERSION

ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_iscc() -> str | None:
    for path in ISCC_CANDIDATES:
        if os.path.isfile(path):
            return path
    return shutil.which("ISCC") or shutil.which("iscc")


def _run_pyinstaller(py_exe: str, base_dir: str, spec: str) -> None:
    print(f"\n--- PyInstaller: {spec} ---")
    ret = subprocess.run(
        [py_exe, "-m", "PyInstaller", "--noconfirm", "--clean", spec],
        cwd=base_dir,
    )
    if ret.returncode != 0:
        print(f"[ERROR] PyInstaller failed for {spec} (code {ret.returncode})")
        sys.exit(ret.returncode)


def build():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    build_dir = os.path.join(base_dir, "build")
    deployment_dir = os.path.join(base_dir, "dist_deployment")
    zip_path = os.path.join(base_dir, "dist_deployment.zip")
    update_zip = os.path.join(
        base_dir, "installer_output", f"Tallylive_python_sync_background_{APP_VERSION}.zip"
    )
    iss_path = os.path.join(base_dir, "installer", "TallyLiveSync.iss")
    installer_out = os.path.join(base_dir, "installer_output")

    print("=== TALLYLIVE BUILD & INSTALLER PACKAGING ===")
    print(f"APP_VERSION = {APP_VERSION}")

    wrapper_files = [
        "myserviceinstaller.exe",
        "myserviceinstaller.xml",
        "install_service.bat",
        "uninstall_service.bat",
        "kill_ghosts.bat",
    ]
    backup_cache = {}
    for wf in wrapper_files:
        path = os.path.join(deployment_dir, wf)
        if os.path.exists(path):
            with open(path, "rb") as f:
                backup_cache[wf] = f.read()
            print(f"[OK] Backed up wrapper: {wf} ({len(backup_cache[wf])} bytes)")
        else:
            print(f"[WARN] Wrapper not found: {wf}")

    print("\n--- Clearing build, dist, and dist_deployment folders ---")
    for path in (dist_dir, build_dir, deployment_dir):
        if os.path.exists(path):
            print(f"Removing {path}...")
            shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    os.makedirs(deployment_dir, exist_ok=True)

    # Keep installer_output clean: only current Setup + versioned update zip (+ sha256)
    if os.path.exists(installer_out):
        print(f"Clearing {installer_out}...")
        shutil.rmtree(installer_out, ignore_errors=True)
    os.makedirs(installer_out, exist_ok=True)

    for wf, content in backup_cache.items():
        with open(os.path.join(deployment_dir, wf), "wb") as f:
            f.write(content)
        print(f"[OK] Restored wrapper: {wf}")

    py_exe = sys.executable
    _run_pyinstaller(py_exe, base_dir, "Tallylive_python_sync_background.spec")
    _run_pyinstaller(py_exe, base_dir, "Tallylive_sync_settings.spec")
    _run_pyinstaller(py_exe, base_dir, "Tallylive_updater.spec")

    built_sync = os.path.join(dist_dir, "Tallylive_python_sync_background")
    built_settings = os.path.join(dist_dir, "Tallylive_sync_settings.exe")
    built_updater = os.path.join(dist_dir, "Tallylive_updater.exe")

    for path, label in (
        (built_sync, "sync folder"),
        (built_settings, "settings EXE"),
        (built_updater, "updater EXE"),
    ):
        if not os.path.exists(path):
            print(f"[ERROR] Missing {label}: {path}")
            sys.exit(1)

    print("\n--- Assembling dist_deployment ---")
    target_sync = os.path.join(deployment_dir, "Tallylive_python_sync_background")
    shutil.copytree(built_sync, target_sync)
    shutil.copy2(built_settings, os.path.join(deployment_dir, "Tallylive_sync_settings.exe"))
    shutil.copy2(built_updater, os.path.join(deployment_dir, "Tallylive_updater.exe"))

    allowlist_src = os.path.join(base_dir, "installer", "IT_ALLOWLIST.txt")
    if os.path.isfile(allowlist_src):
        # Installed with the product via Inno; also in dist_deployment for zip deploys
        shutil.copy2(allowlist_src, os.path.join(deployment_dir, "IT_ALLOWLIST.txt"))
        print("[OK] Copied IT_ALLOWLIST.txt -> dist_deployment")
    else:
        print("[WARN] installer/IT_ALLOWLIST.txt not found")

    # Ship a template .env (installer overwrites with wizard values)
    env_src = os.path.join(base_dir, ".env")
    env_dst = os.path.join(target_sync, ".env")
    if os.path.isfile(env_src):
        shutil.copy2(env_src, env_dst)
        print(f"[OK] Copied .env -> {target_sync}")
    else:
        with open(env_dst, "w", encoding="utf-8") as f:
            f.write(
                "TALLY_HOST=localhost\n"
                "TALLY_PORT=9000\n"
                "SYNC_INTERVAL=3600\n"
                "FULL_RECONCILE=0\n"
            )
        print("[OK] Wrote default .env template")

    # Legacy full deployment zip
    print("\n--- Creating dist_deployment.zip ---")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _dirs, files in os.walk(deployment_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, os.path.dirname(deployment_dir))
                zipf.write(abs_path, rel_path)
    print(f"[OK] {zip_path}")

    # Auto-update zip (sync folder only — no .env/data)
    print("\n--- Creating auto-update zip ---")
    if os.path.isfile(update_zip):
        os.remove(update_zip)
    with zipfile.ZipFile(update_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(target_sync):
            # skip runtime junk if present
            dirs[:] = [d for d in dirs if d.lower() not in ("data",)]
            for file in files:
                if file.lower() in (".env", "sync.log", "sync_cache.db"):
                    continue
                abs_path = os.path.join(root, file)
                # Zip with top-level folder name for updater strip logic
                rel_inside = os.path.relpath(abs_path, deployment_dir)
                zipf.write(abs_path, rel_inside.replace("\\", "/"))
    digest = _sha256(update_zip)
    # Version is in the zip filename, e.g. Tallylive_python_sync_background_1.1.1.zip
    zip_basename = os.path.basename(update_zip)
    storage_path = f"{APP_VERSION}/{zip_basename}"
    print(f"[OK] {update_zip}")
    print(f"[OK] SHA256 = {digest}")
    print(f"[OK] Upload to Storage as: sync-releases/{storage_path}")
    sha_file = update_zip + ".sha256.txt"
    with open(sha_file, "w", encoding="utf-8") as f:
        f.write(digest + "\n")
        f.write(f"version={APP_VERSION}\n")
        f.write(f"zip_filename={zip_basename}\n")
        f.write(f"storage_path={storage_path}\n")
    print(f"[OK] Wrote {sha_file}")

    # Inno Setup
    print("\n--- Compiling Inno Setup installer ---")
    iscc = _find_iscc()
    if not iscc:
        print("[WARN] ISCC.exe not found — skipping Inno compile.")
        print("       Install Inno Setup 6 or add ISCC to PATH.")
    elif not os.path.isfile(iss_path):
        print(f"[ERROR] Missing ISS: {iss_path}")
        sys.exit(1)
    else:
        # Keep ISS version in sync with APP_VERSION (light touch replace)
        with open(iss_path, "r", encoding="utf-8") as f:
            iss_text = f.read()
        import re

        iss_text_new, n = re.subn(
            r'#define MyAppVersion ".*?"',
            f'#define MyAppVersion "{APP_VERSION}"',
            iss_text,
            count=1,
        )
        if n:
            with open(iss_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(iss_text_new)
            print(f"[OK] Synced ISS version to {APP_VERSION}")

        ret = subprocess.run([iscc, iss_path], cwd=base_dir)
        if ret.returncode != 0:
            print(f"[ERROR] Inno Setup compile failed (code {ret.returncode})")
            sys.exit(ret.returncode)
        setup_exe = os.path.join(
            installer_out, f"Setup_TallyLive_Sync_{APP_VERSION}.exe"
        )
        if os.path.isfile(setup_exe):
            print(f"[SUCCESS] Installer: {setup_exe}")
        else:
            print(f"[WARN] Expected installer not found at {setup_exe}")

    print("\n=== BUILD & PACKAGING COMPLETED SUCCESSFULLY ===")
    print(f"Deployment folder : {deployment_dir}")
    print(f"Update zip        : {update_zip}")
    print(f"Update SHA256     : {digest}")
    print(f"Storage path      : {storage_path}")
    print("Next: run installer/supabase_sync_app_releases.sql in Supabase if not done.")
    print("Then upload the versioned zip to Storage bucket sync-releases and activate the row.")


if __name__ == "__main__":
    build()
