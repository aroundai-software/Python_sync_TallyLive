-- Run in Supabase SQL Editor (once).

-- 1) Release metadata for auto-update
create table if not exists public.sync_app_releases (
  id uuid primary key default gen_random_uuid(),
  version text not null,
  storage_path text not null,
  sha256 text not null,
  is_active boolean not null default false,
  mandatory boolean not null default true,
  released_at timestamptz not null default now(),
  notes text
);

create unique index if not exists sync_app_releases_version_uidx
  on public.sync_app_releases (version);

-- Only one active release at a time (optional soft rule — enforce in app too)
create index if not exists sync_app_releases_active_idx
  on public.sync_app_releases (is_active);

-- 2) Track installed version per machine (optional; sync already falls back if missing)
alter table public.script_control
  add column if not exists app_version text;

alter table public.script_control
  add column if not exists last_update_check_at timestamptz;

-- Optional: show app version on sync_logs (sync falls back if column missing)
alter table public.sync_logs
  add column if not exists app_version text;

-- 3) RLS — sync EXE uses the anon key; without SELECT policy it sees zero rows.
alter table public.sync_app_releases enable row level security;

drop policy if exists "sync_app_releases_select_anon" on public.sync_app_releases;
create policy "sync_app_releases_select_anon"
  on public.sync_app_releases
  for select
  to anon, authenticated
  using (true);

-- Clients must NOT insert/update/delete releases (do that in Dashboard / service role).
-- No write policies for anon = writes blocked when RLS is on. Good.

-- 4) Storage bucket "sync-releases" (private) + download policy for anon
insert into storage.buckets (id, name, public)
values ('sync-releases', 'sync-releases', false)
on conflict (id) do nothing;

drop policy if exists "sync_releases_select_anon" on storage.objects;
create policy "sync_releases_select_anon"
  on storage.objects
  for select
  to anon, authenticated
  using (bucket_id = 'sync-releases');

-- Upload new zips via Dashboard (or service role). Do not allow anon INSERT.

-- 5) After uploading a zip, activate a release row, e.g.:
--    Zip filename includes version: Tallylive_python_sync_background_1.1.1.zip
-- insert into public.sync_app_releases (version, storage_path, sha256, is_active, notes)
-- values (
--   '1.1.1',
--   '1.1.1/Tallylive_python_sync_background_1.1.1.zip',
--   '<sha256>',
--   true,
--   'release notes'
-- );
-- update public.sync_app_releases set is_active = false where version <> '1.1.1';
