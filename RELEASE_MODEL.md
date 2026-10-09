# Release model — installer, Settings, auto-update

Reusable pattern used by **TallyLive Sync** so other sync apps can ship the same way: Inno Setup installer, Settings UI, IT allowlist, and Supabase-hosted auto-update.

TallyLive-specific sync logic (Tally modules, tables) is out of scope here. Copy the **packaging and update** design.

---

## 1. What you ship

| Artifact | Who gets it | Purpose |
|----------|-------------|---------|
| `Setup_YourApp_{ver}.exe` | New client PCs | First install (Inno Setup) |
| `YourApp_{ver}.zip` | You only (Supabase Storage) | Auto-update payload |
| `YourApp_{ver}.zip.sha256.txt` | You only | Hash for `sync_app_releases` |

Clients never need the zip. Existing installs pull it via auto-update.

---

## 2. Repo layout (copy this)

```
your_sync_app/
├── src/
│   ├── version.py          # APP_VERSION = "x.y.z" (single source of truth)
│   ├── auto_update.py      # active release → download → SHA256 → launch updater
│   ├── paths.py            # install root, app dir, updater path
│   ├── env_file.py         # read/write .env
│   └── sync_logger.py      # optional: sync_logs.app_version
├── settings_ui/
│   └── settings_main.py    # host/port/interval; shows Version
├── updater/
│   └── updater_main.py     # stop service → backup → extract → preserve data → start / rollback
├── installer/
│   ├── YourApp.iss         # Inno Setup
│   ├── IT_ALLOWLIST.txt    # wizard + installed copy
│   ├── your_app.ico
│   └── supabase_sync_app_releases.sql
├── *.spec                  # PyInstaller: main (onedir), settings (onefile), updater (onefile)
├── build_installer.py      # build all → zip → Setup.exe → sha256
├── dist_deployment/        # assembled payload for Inno
└── installer_output/       # Setup + update zip + .sha256.txt
```

**Reference files in this repo**

| File | Role |
|------|------|
| `src/version.py` | Version constant |
| `src/auto_update.py` | Release check + download |
| `updater/updater_main.py` | Apply update |
| `settings_ui/settings_main.py` | Settings + version label |
| `installer/TallyLiveSync.iss` | Inno + config wizard + service |
| `installer/IT_ALLOWLIST.txt` | Firewall / AV guide |
| `installer/supabase_sync_app_releases.sql` | Table + RLS + bucket |
| `build_installer.py` | Full release pipeline |
| `test_auto_update.py` | Version-compare tests |

---

## 3. Three binaries (always)

| Binary | PyInstaller | Role |
|--------|-------------|------|
| Main sync | **onedir** (EXE + `_internal`) | Windows service / background sync |
| Settings | **onefile** | Edit `.env`; Start Menu shortcut |
| Updater | **onefile** | Replace files after download |

Inno installs all three under **one** `AppId` → one entry in Apps & features. Settings is not a separately uninstallable product.

---

## 4. Supabase release channel

### Table `sync_app_releases`

| Column | Meaning |
|--------|---------|
| `version` | e.g. `1.1.3` (unique) |
| `storage_path` | e.g. `1.1.3/YourApp_1.1.3.zip` |
| `sha256` | Hex hash of that zip |
| `is_active` | Only **one** row `true` at a time |
| `mandatory` / `notes` / `released_at` | Optional |

### Storage bucket `sync-releases` (private)

```
sync-releases/
  1.1.1/YourApp_1.1.1.zip
  1.1.3/YourApp_1.1.3.zip
```

Upload path creates the version folder; no empty folder required beforehand.

### RLS

- **anon / authenticated:** `SELECT` on `sync_app_releases` + download objects from `sync-releases`
- **No anon INSERT/UPDATE/DELETE** — publish releases from Dashboard / service role only

Bootstrap: `installer/supabase_sync_app_releases.sql`.  
For a separate product, use a dedicated table and/or bucket name if you need isolation.

Optional: `script_control.app_version`, `sync_logs.app_version` for ops visibility.

---

## 5. Auto-update (client flow)

```
Sync cycle finishes (idle)
  → SELECT active row from sync_app_releases
  → if remote version > local APP_VERSION
      → download zip from Storage (storage_path)
      → verify SHA256 (reject on mismatch)
      → write update_pending.json
      → start updater.exe; main process exits
Updater
  → stop Windows service
  → backup current app folder
  → extract zip over program files
  → PRESERVE: .env, data/ (incl. local DB), logs
  → start service — or rollback on failure
```

**Rules**

- Version compare is numeric `x.y.z` (`1.1.3` > `1.1.2`)
- Update zip must not overwrite live client config/DB as source of truth
- Confirm success: Settings shows new version, or `sync_logs.app_version` for that machine

Wire the check at the end of an idle cycle (see `main.py` → `check_and_apply_update()`).

---

## 6. Inno Setup responsibilities

- Install under `{autopf}\Your App Name`
- Copy: service wrapper + main onedir + settings + updater + `IT_ALLOWLIST.txt`
- **InfoBeforeFile:** show `IT_ALLOWLIST.txt` before install
- **Custom wizard page:** host / port / interval / full-reconcile → write `.env` post-install
- **Run:** service `install` + `start`
- **Uninstall:** service `stop` + `uninstall`
- Unique `AppId` (GUID) **per product** — do not reuse another app’s `AppId`

---

## 7. Build pipeline (`build_installer.py`)

One command should:

1. Read `APP_VERSION` from `src/version.py`
2. Run PyInstaller for main, settings, updater
3. Assemble `dist_deployment/` (preserve service wrapper binaries)
4. Create update zip → `installer_output/YourApp_{ver}.zip`
5. Write SHA256 → `*.zip.sha256.txt`
6. Sync ISS `#define MyAppVersion` to match
7. Compile Inno → `Setup_YourApp_{ver}.exe`

Requires: Python venv + deps, PyInstaller, [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`ISCC.exe`).

---

## 8. Release checklist (every version)

1. Bump `APP_VERSION` in `src/version.py` (and ISS version / let build sync it)
2. Run `python build_installer.py`
3. Upload zip to Storage: `sync-releases/{ver}/YourApp_{ver}.zip`
4. Activate release in SQL (only this version `is_active = true`; SHA from `.sha256.txt`)
5. **New PCs:** ship `Setup_YourApp_{ver}.exe` only
6. **Existing PCs:** wait for next sync cycle; confirm `app_version`
7. Keep ~2–3 zips in Storage; delete older folders if you do not need rollback

### Example activate SQL

```sql
update public.sync_app_releases
set is_active = false
where version <> '1.1.3';

insert into public.sync_app_releases (
  version, storage_path, sha256, is_active, mandatory, notes
)
values (
  '1.1.3',
  '1.1.3/YourApp_1.1.3.zip',
  '<sha256 from .sha256.txt>',
  true,
  true,
  'release notes'
)
on conflict (version) do update
set
  storage_path = excluded.storage_path,
  sha256 = excluded.sha256,
  is_active = true,
  mandatory = excluded.mandatory,
  notes = excluded.notes,
  released_at = now();
```

---

## 9. IT allowlist contents

Document in `IT_ALLOWLIST.txt` (and show in the wizard):

- EXE names: main, settings, updater, service wrapper
- Outbound HTTPS to Supabase
- Local ports (e.g. Tally) if the app uses them
- Suggested AV exclusions for the install folder

---

## 10. Reuse vs customize

| Reuse the pattern | Change per app |
|-------------------|----------------|
| version + auto_update + updater + settings + Inno + build script + release SQL | App name, paths, service name, `.env` keys |
| SHA verify + preserve `.env` / `data` / logs | PyInstaller specs, icons |
| Single Setup + update zip + one active release | New Inno `AppId` GUID |
| | Bucket/table names if products must be isolated |
| | Business sync modules / tables |

---

## 11. How to verify an update reached a client

1. **Supabase:** `sync_logs.app_version` for that `machine_name` / `company_name`
2. **Client:** open Settings → version label at top
3. **Client log:** install folder `logs\update.log` (download / apply / restart)

Auto-update runs after a sync cycle when remote version is newer — not instantly when you run the SQL.

---

## 12. Related docs

- Architecture and sync behavior: `README.md`
- End-user defaults and settings: `README_DEFAULTS.md`
