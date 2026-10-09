"""
TallyLive updater helper.
Stops the service, swaps sync folder contents from a verified zip,
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


def extract_update(zip_path: str, target_dir: str, log_path: str) -> None:
    """
    Extract zip into target_dir.
    Zip may contain either flat files or a top-level Tallylive_python_sync_background/ folder.
    Preserves .env, data/, sync.log.
    """
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

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        top_levels = {n.split("/")[0].split("\\")[0] for n in names if n and not n.endswith("/")}
        strip_prefix = None
        if len(top_levels) == 1:
            only = next(iter(top_levels))
            if only.lower() == "tallylive_python_sync_background":
                strip_prefix = only + "/"

        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            rel = name
            if strip_prefix and rel.startswith(strip_prefix):
                rel = rel[len(strip_prefix) :]
            # Never overwrite preserved from zip if present
            top = rel.split("/")[0]
            if top in PRESERVE_NAMES:
                continue
            dest = os.path.join(target_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

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

    log("Extract + preserve complete.", log_path)


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

    # Give sync process time to exit
    time.sleep(3)
    bak = None
    try:
        stop_service(install_root, log_path)
        time.sleep(2)
        bak = backup_dir(target_dir, log_path)
        extract_update(zip_path, target_dir, log_path)
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
