# TallyLive Sync — Default settings (simple guide)

This page lists the **standard values** that TallyLive Sync uses when you don’t change anything.  
We will add more here over time.

For technical architecture, see `README.md`.

---

## What is this?

When you install TallyLive Sync, it comes with ready-made settings so it can start working with Tally and the cloud.  
You can change many of these later in **TallyLive Sync Settings** (Start Menu).

---

## Connection to Tally (defaults)

| Setting | Default | What it means |
|---------|---------|----------------|
| Tally computer | `localhost` | Sync looks for Tally on **this same PC** |
| Port | `9000` | The usual Tally HTTP / ODBC port |
| Full reconcile | **Off** | Normal day-to-day sync only picks up **new/changed** data (faster). Turn **On** only when you need a full re-check |

**Tip:** If Tally is on another machine, change the host in Settings. Port stays `9000` unless your Tally is set differently.

---

## How often it syncs (defaults)

| Setting | Default | What it means |
|---------|---------|----------------|
| Sync interval (in Settings) | **60 minutes** when installed with the Setup wizard | Sync runs, then waits about one hour, then runs again |
| If interval is missing or invalid | About **30 minutes** | Built-in safety fallback |

You can change the interval anytime in **TallyLive Sync Settings** (shown in minutes).  
Example: set `30` → sync about every half hour.

---

## After install — what you get by default

| Item | Default |
|------|---------|
| Program name | TallyLive Sync |
| Version (current package) | 1.1.1 |
| Install location | `C:\Program Files\TallyLive Sync\` |
| Runs as | Windows service in the **background** (starts with Windows) |
| Settings shortcut | In the **Start Menu** |
| Desktop icon | **Optional** (you choose during Setup; off by default) |

---

## Cloud / account defaults

| Setting | Default | What it means |
|---------|---------|----------------|
| Cloud project | Built into the app | You normally **don’t** enter Supabase details |
| License / validity for a new machine | Often **14 days** unless set otherwise | Admin can extend this in the cloud control panel |
| Remote pause | Sync is **allowed to run** | Admin can pause a machine from the cloud if needed |

---

## What happens if something goes wrong (defaults)

| Situation | What Sync does |
|-----------|----------------|
| Internet is down | Still reads from Tally and saves on this PC; uploads to the cloud when the network is back |
| One step fails | Other steps can still continue; you may see **partial** status in cloud logs |
| You turn on Full reconcile | Runs a heavier full pass **once**, then turns itself **Off** again |

---

## Auto-update (defaults)

| Item | Default |
|------|---------|
| Updates from | TallyLive cloud (when we publish a new version) |
| Your settings & local data | **Kept** (not wiped by an update) |
| When it checks | After a normal sync finishes |

You usually **don’t** need to reinstall for small updates if the PC already has Sync installed.

---

## Quick “leave it as default” checklist

For a normal single-PC setup:

1. Tally is on the **same computer**  
2. Tally HTTP is on port **9000**  
3. Leave host as **localhost**  
4. Leave full reconcile **Off**  
5. Interval **60 minutes** (or change later if you want faster/slower)

---

## Changing defaults later

Open **Start → TallyLive Sync Settings**:

- Tally host  
- Port  
- Sync interval (minutes)  
- Full reconcile (one-time)

Click **Save**. Changes apply on the **next** sync.

---

## What data Sync fetches, and from when to when

### Date window (vouchers / daybook)
By default Sync uses the **Indian financial year**:

- **From:** 1 April of the current FY  
- **To:** **today**

Example (if today is 7 Oct 2026): **1 Apr 2026 → 7 Oct 2026**.

Optional: set `SYNC_START_YEAR` (e.g. `2025`) to sync that full FY only (**1 Apr 2025 → 31 Mar 2026**).

Sales/purchase **bills** are requested in **month-by-month** chunks inside that window.  
Sale/purchase **line items** use the **whole FY window** in one go (still limited by Alter ID when possible).

### What each step fetches

| Data | From Tally | Date / scope |
|------|------------|----------------|
| Company | Company details | Current open company |
| Customers & suppliers | Ledgers (party masters) | Masters (not a voucher date range); balances as of today |
| Godowns | Stock locations | Masters |
| Cost centers | Cost centres | Masters |
| Stock items | Item masters + qty/rate | Masters; stock position roughly from early date → **today** |
| Sales invoices | Sales vouchers | **Current FY start → today** (monthly chunks) + Alter ID filter when possible |
| Sales invoice lines | Inventory lines on sales vouchers | Same FY window + Alter ID |
| Purchase invoices | Purchase vouchers | **Current FY start → today** (monthly chunks) + Alter ID |
| Purchase invoice lines | Inventory lines on purchases | Same FY window + Alter ID |
| Outstanding receivables | Bills receivable / ageing style export | Broad history (not limited to current FY only) |
| Outstanding payables | Bills payable style export | Broad history |
| Invoice paid/pending status | Derived from outstanding vs invoices | Same company |
| Daybook | Vouchers for daybook | **Current FY start → today** (monthly) + Alter ID |
| Settled bills | Fully settled bill references | Tied to current FY / payment activity + Alter ID |

### Masters vs vouchers (simple)
- **Masters** (customers, stock, godowns…): not “from April to today” in the voucher sense — Sync loads the master list (often only changed ones via Alter ID).  
- **Vouchers** (sales, purchase, daybook…): limited to the **FY window** above, then further reduced by **Alter ID** on later runs.

### What Sync does *not* mean by default
It does **not** pull every voucher from the beginning of the Tally company forever — only the **current financial year up to today** (unless you set `SYNC_START_YEAR` or use Full reconcile / first-run behaviour for a module).

---

## How Alter ID works (incremental sync)

### What is Alter ID?
In Tally, every master or voucher gets a number called **Alter ID**.  
When something is **created or changed**, that number goes **up**.

TallyLive Sync uses this so it does **not** download everything every time.

### Simple idea
1. Sync remembers the **highest Alter ID** it already stored for that company (on this PC).  
2. Next time it asks Tally: “Give me only items with Alter ID **greater than** that number.”  
3. So only **new or edited** records come across.  
4. First run (or empty local data) has no Alter ID yet → Sync may pull a **full** set for that module.  
5. If you turn on **Full reconcile**, Sync temporarily forgets the watermark and pulls as if from the start (once), then goes back to normal.

### Everyday example
| Day | What happens |
|-----|----------------|
| Monday (first sync) | Local store empty → pull a lot of data; remember highest Alter ID, e.g. **5000** |
| Tuesday | Only vouchers/items with Alter ID **> 5000** are fetched |
| You edit an old invoice | That invoice’s Alter ID becomes higher → it is fetched again and updated |
| You create a new invoice | New Alter ID → fetched and added |

### Important
- Alter ID is about **what to fetch from Tally**.  
- Separately, Sync still **inserts/updates/deletes** in its local store and in the cloud (see below).  
- Some steps (like live stock balances or outstanding lists) may refresh more than “Alter ID only” because those numbers change often.

---

## How data is inserted, updated (upserted), and deleted

Think of three places: **Tally** → **this PC (local store)** → **TallyLive cloud**.

### 1) Insert / update on this PC (after reading Tally)
When Sync gets records from Tally, it saves them locally using the record’s **GUID** (unique ID) plus **company name**.

| Situation | What Sync does on the PC |
|-----------|---------------------------|
| New GUID | **Insert** a new row |
| Same GUID already exists | **Update** that row with the new values |
| Combined | This “insert or update” is called an **upsert** |

After saving locally, the row is marked **pending** until it is uploaded to the cloud.

### 2) Upload to the cloud (upsert)
When the network part runs, pending rows are sent to Supabase.

| Situation | What happens in the cloud |
|-----------|---------------------------|
| GUID (+ company) not there yet | **Insert** |
| GUID (+ company) already there | **Update** (overwrite with latest from Sync) |

So editing a bill in Tally does **not** create a duplicate in the cloud if the GUID is the same — it **updates** the existing row.

### 3) Delete
When a voucher (or master) is **deleted/cancelled** in Tally:

1. Sync notices it is no longer in Tally’s active list (or sees deleted/cancelled while fetching).  
2. Removes it from the **local** store and queues a cloud delete.  
3. On upload, the cloud row is **deleted** (for invoices, line items are removed first, then the invoice).  

Safety: if Tally returns an empty list by mistake, Sync **won’t** wipe all cloud data that cycle.

### One picture

```
Tally change
    ↓
Fetch only higher Alter IDs (usually)
    ↓
Local PC: upsert by company + GUID  (or delete if gone)
    ↓
Cloud: upsert same key  (or delete if queued)
```

---

## How Sync handles deleted vouchers in Tally

When someone **deletes** or **cancels** a voucher in Tally (for example a sales or purchase invoice), TallyLive Sync is designed to remove it from the cloud as well — not leave old bills behind.

### Simple flow

1. **You delete/cancel the voucher in Tally**  
2. On the **next sync**, Sync checks Tally for which vouchers are still active  
3. Anything that was saved earlier but is **no longer active in Tally** is treated as deleted  
4. Sync removes it from the **local copy on the PC**  
5. When it uploads to the cloud, it **deletes that bill in Supabase** too  
6. For sales/purchase invoices, related **line items** are removed first, then the invoice itself  

### What counts as “gone” in Tally

For sales and purchase bills, Sync treats a voucher as removed if Tally marks it as:

- **Deleted**, or  
- **Cancelled**

Those vouchers are not kept as normal live invoices.

### Safety rule (important)

If Sync cannot read the voucher list from Tally properly, or Tally suddenly returns **zero** active bills, Sync **does not wipe** all cloud invoices.  
It **skips** mass deletion that cycle to avoid accidentally clearing everything.

### When it happens

Deletion cleanup runs during the normal sync steps for:

- Sales invoices  
- Purchase invoices  
- (Similar cleanup also exists for masters like customers, stock, godowns, and daybook entries)

You do **not** need a special button — it happens automatically on the next successful sync after the delete in Tally.

### What you may notice

| In Tally | After next sync |
|----------|------------------|
| Invoice deleted/cancelled | Same invoice disappears from TallyLive cloud (for that company) |
| Internet was down when deleted | Removed locally first; cloud delete happens when upload works again |

---

## Security features in Sync

These are the main protections built into TallyLive Sync today:

### 1. Remote control (cloud)
| Feature | What it does |
|---------|----------------|
| **Pause sync** | Admin can turn sync **off** for a machine in the cloud (`script_control`) without visiting the PC |
| **License / expiry date** | Sync stops when the subscription end date is passed |
| **Machine identity** | Each PC reports its **computer name**, so control is per machine |
| **Heartbeat** | Cloud sees the machine was recently online (last seen time) |

### 2. Install & access on the PC
| Feature | What it does |
|---------|----------------|
| **Admin install** | Setup must run as **Administrator** (needed for the Windows service) |
| **Background service** | Sync runs as a Windows service (not a random open window) |
| **Settings not separately uninstallable** | Users can’t remove only Settings and leave a broken install; one product uninstall |
| **IT allowlist in Setup** | Firewall/antivirus guidance shown during install and saved on the PC |

### 3. Auto-update safety
| Feature | What it does |
|---------|----------------|
| **SHA256 check** | Update file must match the hash published in the cloud — corrupted/tampered zip is **rejected** |
| **Only active release** | Clients only take the version marked **active** in the cloud |
| **Backup before replace** | Old program folder is backed up before installing the update |
| **Rollback** | If the new build looks broken, Sync can restore the previous copy |
| **Keeps your data** | Update does **not** wipe `.env` settings or the local database |
| **Update when idle** | Checks for updates **after** a sync finishes, not in the middle of reading Tally |

### 4. Data safety
| Feature | What it does |
|---------|----------------|
| **Company separation** | Each company’s data is kept separate by company name |
| **Local store if cloud is down** | Data is saved on the PC first; upload retries later |
| **Delete safety** | Won’t wipe all cloud invoices if Tally returns an empty list by mistake |
| **Cloud errors visible** | Failures are written to `sync_logs` so you can see the reason remotely |

### 5. Cloud / database side (with your Supabase setup)
| Feature | What it does |
|---------|----------------|
| **Private update bucket** | Update zips are in a **private** Storage bucket |
| **Read-only policies for clients** | Clients can read release info / download updates; they should **not** be able to publish fake releases |
| **Row Level Security** | Enabled on release table (and expected on business tables) so access is controlled |

### Not included yet (optional later)
| Item | Note |
|------|------|
| **Code signing** of Setup/EXE | Reduces Windows SmartScreen / antivirus warnings — recommended for production |
| **Encrypted local database** | Local SQLite is a normal file on the PC (protect the machine login) |

---

## Notes for us (internal)

| Topic | Remember |
|-------|----------|
| Installer default interval | 60 minutes |
| Code safety fallback if interval broken | 30 minutes |
| Example file for developers | may show 60 minutes / 1 year validity — not always what Setup uses |
| Voucher delete path | Reconcile active GUIDs → stage local deletion → Phase 3 deletes children then parent in Supabase |

---

## Updates to this page

| Date | What we added |
|------|----------------|
| 7 Oct 2026 | First simple defaults guide (version 1.1.1) |
| 7 Oct 2026 | How Sync handles deleted/cancelled vouchers from Tally |
| 7 Oct 2026 | Alter ID incremental sync + insert / upsert / delete explained |
| 7 Oct 2026 | What data is fetched and FY date window (Apr → today) |
| 7 Oct 2026 | Security features list (remote control, update hash, data safety) |
