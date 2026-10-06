import os
import shutil
import subprocess
import sys
import zipfile

def build():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    build_dir = os.path.join(base_dir, "build")
    deployment_dir = os.path.join(base_dir, "dist_deployment")
    zip_path = os.path.join(base_dir, "dist_deployment.zip")
    
    print("=== TALLYLIVE BUILD & INSTALLER PACKAGING ===")
    
    # 1. Backup deployment wrapper files
    wrapper_files = [
        "myserviceinstaller.exe",
        "myserviceinstaller.xml",
        "install_service.bat",
        "uninstall_service.bat",
        "kill_ghosts.bat"
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

    # 2. Completely clean dist, build, and dist_deployment
    print("\n--- Clearing build, dist, and dist_deployment folders ---")
    if os.path.exists(dist_dir):
        print(f"Removing {dist_dir}...")
        shutil.rmtree(dist_dir, ignore_errors=True)
    if os.path.exists(build_dir):
        print(f"Removing {build_dir}...")
        shutil.rmtree(build_dir, ignore_errors=True)
    if os.path.exists(deployment_dir):
        print(f"Removing {deployment_dir}...")
        shutil.rmtree(deployment_dir, ignore_errors=True)
    if os.path.exists(zip_path):
        print(f"Removing {zip_path}...")
        os.remove(zip_path)
        
    os.makedirs(deployment_dir, exist_ok=True)
    
    # Restore wrapper files to dist_deployment
    for wf, content in backup_cache.items():
        with open(os.path.join(deployment_dir, wf), "wb") as f:
            f.write(content)
        print(f"[OK] Restored wrapper: {wf}")

    # 3. Build executable with PyInstaller
    print("\n--- Running PyInstaller for Tallylive_python_sync_background ---")
    py_exe = sys.executable
    cmd = [py_exe, "-m", "PyInstaller", "--clean", "Tallylive_python_sync_background.spec"]
    ret = subprocess.run(cmd, cwd=base_dir)
    if ret.returncode != 0:
        print(f"[ERROR] PyInstaller build failed with return code {ret.returncode}")
        sys.exit(ret.returncode)
    
    built_output = os.path.join(dist_dir, "Tallylive_python_sync_background")
    if not os.path.exists(built_output):
        print(f"[ERROR] Built directory does not exist: {built_output}")
        sys.exit(1)
        
    print(f"[SUCCESS] PyInstaller build complete at: {built_output}")

    # 4. Copy to dist_deployment
    print("\n--- Copying built files to dist_deployment ---")
    target_sync_dir = os.path.join(deployment_dir, "Tallylive_python_sync_background")
    shutil.copytree(built_output, target_sync_dir)
    print(f"[OK] Copied {built_output} -> {target_sync_dir}")

    # Copy .env file
    env_file = os.path.join(base_dir, ".env")
    if os.path.exists(env_file):
        shutil.copy2(env_file, os.path.join(target_sync_dir, ".env"))
        print(f"[OK] Copied .env -> {target_sync_dir}")
    else:
        print("[WARN] .env file not found in root")

    # 5. Create zip archive
    print("\n--- Creating dist_deployment.zip ---")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(deployment_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, os.path.dirname(deployment_dir))
                zipf.write(abs_path, rel_path)
    print(f"[OK] Created zip archive at: {zip_path}")
    print(f"[OK] Deployment folder ready at: {deployment_dir}")
    print("\n=== BUILD & PACKAGING COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    build()
