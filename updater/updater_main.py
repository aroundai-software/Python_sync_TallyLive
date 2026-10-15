"""
TallyLive updater helper.
Stops the service, swaps sync folder contents from a verified zip,
updates Settings EXE at install root when present in the zip,
preserves .env and data folder, restarts the service, rolls back on failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import datetime


PRESERVE_NAMES = {".env", "data", "sync.log", "sync_cache.db"}
SYNC_FOLDER = "tallylive_python_sync_background"
# Files at zip root that belong in the install root (beside the sync folder)
ROOT_UPDATE_FILES = {
    "tallylive_sync_settings.exe",
}


def log(msg: str, log_path: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, cwd=None) -> int:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=False)
        return p.returncode
    except Exception:
        return 1


def service_exe(install_root: str) -> str:
    return os.path.join(install_root, "myserviceinstaller.exe")


def stop_service(install_root: str, log_path: str) -> None:
    exe = service_exe(install_root)
    if os.path.isfile(exe):
        log("Stopping service...", log_path)
        run([exe, "stop"], cwd=install_root)
        time.sleep(2)


def start_service(install_root: str, log_path: str) -> None:
    exe = service_exe(install_root)
    if os.path.isfile(exe):
        log("Starting service...", log_path)
        run([exe, "start"], cwd=install_root)


def backup_dir(target_dir: str, log_path: str) -> str:
    bak = target_dir.rstrip("\\/") + ".bak"
    if os.path.exists(bak):
        shutil.rmtree(bak, ignore_errors=True)
    log(f"Backing up {target_dir} -> {bak}", log_path)
    shutil.copytree(target_dir, bak)
    return bak


def restore_backup(bak: str, target_dir: str, log_path: str) -> None:
    log(f"Rolling back from {bak}", log_path)
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    shutil.copytree(bak, target_dir)


def _replace_file(src_tmp: str, dest: str, log_path: str) -> None:
    """Replace dest with src_tmp; retry briefly if Settings is open."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    for attempt in range(5):
        try:
            if os.path.isfile(dest):
                try:
                    os.remove(dest)
                except OSError:
                    # Try rename-aside then replace
                    aside = dest + ".old"
                    try:
                        if os.path.isfile(aside):
                            os.remove(aside)
                        os.replace(dest, aside)
                    except OSError:
                        pass
            shutil.copy2(src_tmp, dest)
            log(f"Updated {os.path.basename(dest)}", log_path)
            # Best-effort cleanup of .old
            aside = dest + ".old"
            if os.path.isfile(aside):
                try:
                    os.remove(aside)
                except OSError:
                    pass
            return
        except OSError as e:
            log(f"Retry replace {os.path.basename(dest)} ({e})", log_path)
            time.sleep(1)
    raise RuntimeError(f"Could not replace {dest} (is Settings still open?)")


def extract_update(
    zip_path: str,
    target_dir: str,
    log_path: str,
    install_root: str | None = None,
) -> None:
    """
    Extract zip into target_dir (sync folder).
    Also copies root-level Settings EXE into install_root when present.
    Zip layouts supported:
      - Tallylive_python_sync_background/...  (+ optional Tallylive_sync_settings.exe)
      - flat files (legacy) into target_dir
    Preserves .env, data/, sync.log inside the sync folder.
    """
    if not install_root:
        install_root = os.path.dirname(target_dir.rstrip("\\/"))

    preserved = {}
    for name in PRESERVE_NAMES:
        src = os.path.join(target_dir, name)
        if os.path.exists(src):
            tmp = os.path.join(os.path.dirname(target_dir), f"_preserve_{name}")
            if os.path.isdir(src):
                if os.path.exists(tmp):
                    shutil.rmtree(tmp, ignore_errors=True)
                shutil.copytree(src, tmp)
            else:
                shutil.copy2(src, tmp)
            preserved[name] = tmp

    # Clear replaceable contents (keep folder)
    for entry in list(os.listdir(target_dir)):
        if entry in PRESERVE_NAMES:
            continue
        path = os.path.join(target_dir, entry)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass

    root_updates: list[tuple[str, str]] = []  # (zip name, basename)

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n.replace("\\", "/") for n in zf.namelist() if n and not n.endswith("/")]
        has_sync_prefix = any(
            n.lower().startswith(SYNC_FOLDER + "/") for n in names
        )

        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            lower = name.lower()
            base = os.path.basename(name)

            # Root-level companions (Settings) → install root, not sync folder
            if "/" not in name.rstrip("/") and base.lower() in ROOT_UPDATE_FILES:
                root_updates.append((name, base))
                continue

            rel = name
            if has_sync_prefix:
                prefix = SYNC_FOLDER + "/"
                # Accept either case of folder name in zip
                lower_name = name.lower()
                if lower_name.startswith(prefix):
                    # strip matching prefix length from original
                    rel = name[len(prefix) :]
                elif name.split("/")[0].lower() == SYNC_FOLDER:
                    rel = "/".join(name.split("/")[1:])
                else:
                    # Other top-level paths (ignore unknown)
                    continue
            # Never overwrite preserved from zip if present
            top = rel.split("/")[0]
            if top in PRESERVE_NAMES:
                continue
            if not rel:
                continue
            dest = os.path.join(target_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

        # Apply Settings (and future root files) into install root
        for zip_name, base in root_updates:
            tmp_path = os.path.join(install_root, f"_update_{base}")
            with zf.open(zip_name) as src, open(tmp_path, "wb") as out:
                shutil.copyfileobj(src, out)
            dest = os.path.join(install_root, base)
            try:
                _replace_file(tmp_path, dest, log_path)
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    for name, tmp in preserved.items():
        dest = os.path.join(target_dir, name)
        if os.path.isdir(tmp):
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(tmp, dest)
        else:
            shutil.copy2(tmp, dest)
        try:
            if os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                os.remove(tmp)
        except OSError:
            pass

    # Settings nested for old-updater-compatible zips (single top-level sync folder)
    _promote_settings_payload(target_dir, install_root, log_path)

    log("Extract + preserve complete.", log_path)


def _promote_settings_payload(
    target_dir: str, install_root: str, log_path: str
) -> None:
    """Copy Settings EXE from sync/_update_payload/ to install root, then remove payload."""
    payload_name = "Tallylive_sync_settings.exe"
    payload = os.path.join(target_dir, "_update_payload", payload_name)
    if not os.path.isfile(payload):
        return
    dest = os.path.join(install_root, payload_name)
    tmp_path = os.path.join(install_root, f"_update_{payload_name}")
    try:
        shutil.copy2(payload, tmp_path)
        _replace_file(tmp_path, dest, log_path)
        log(f"Promoted Settings from _update_payload -> {dest}", log_path)
    except Exception as e:
        log(f"Settings promote skipped: {e}", log_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        try:
            shutil.rmtree(os.path.join(target_dir, "_update_payload"), ignore_errors=True)
        except OSError:
            pass


def health_ok(target_dir: str) -> bool:
    exe = os.path.join(target_dir, "Tallylive_python_sync_background.exe")
    return os.path.isfile(exe)


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: Tallylive_updater.exe <update_pending.json>")
        return 2

    pending_path = argv[0]
    try:
        with open(pending_path, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except Exception as e:
        print(f"Cannot read pending file: {e}")
        return 1

    install_root = pending.get("install_root") or os.path.dirname(pending_path)
    target_dir = pending["target_dir"]
    zip_path = pending["zip_path"]
    log_path = os.path.join(install_root, "logs", "update.log")

    log(f"Update start -> {pending.get('version')} from {pending.get('from_version')}", log_path)
    log(f"Zip: {zip_path}", log_path)
    log(f"Target: {target_dir}", log_path)
    log(f"Install root: {install_root}", log_path)

    # Give sync process time to exit
    time.sleep(3)
    bak = None
    try:
        stop_service(install_root, log_path)
        time.sleep(2)
        bak = backup_dir(target_dir, log_path)
        extract_update(zip_path, target_dir, log_path, install_root=install_root)
        if not health_ok(target_dir):
            raise RuntimeError("New EXE missing after extract")
        start_service(install_root, log_path)
        time.sleep(5)
        log("Update completed successfully.", log_path)
        try:
            os.remove(pending_path)
        except OSError:
            pass
        return 0
    except Exception as e:
        log(f"Update FAILED: {e}", log_path)
        log(traceback.format_exc(), log_path)
        if bak and os.path.isdir(bak):
            try:
                restore_backup(bak, target_dir, log_path)
                start_service(install_root, log_path)
                log("Rollback complete.", log_path)
            except Exception as re:
                log(f"Rollback failed: {re}", log_path)
        return 1


if __name__ == "__main__":
    sys.exit(main())
