-- Read-only inventory of policies, functions, triggers, key constraints/columns

SELECT 'POLICIES' AS section;
SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual IS NOT NULL AS has_using, with_check IS NOT NULL AS has_check
FROM pg_policies
WHERE schemaname = 'public'
ORDER BY tablename, policyname;

SELECT 'RLS_ENABLED' AS section;
SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled, c.relforcerowsecurity AS rls_forced
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname;

SELECT 'TRIGGERS' AS section;
SELECT event_object_table AS table_name, trigger_name, action_timing, event_manipulation, action_statement
FROM information_schema.triggers
WHERE trigger_schema = 'public'
ORDER BY event_object_table, trigger_name, event_manipulation;

SELECT 'FUNCTIONS' AS section;
SELECT p.proname AS function_name,
       pg_get_function_identity_arguments(p.oid) AS args,
       CASE p.prosecdef WHEN true THEN 'SECURITY DEFINER' ELSE 'SECURITY INVOKER' END AS security,
       l.lanname AS lang
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = 'public'
  AND p.prokind = 'f'
ORDER BY p.proname, 2;

SELECT 'KEY_CONSTRAINTS' AS section;
SELECT conrelid::regclass::text AS table_name, conname, pg_get_constraintdef(oid) AS def
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace
  AND contype IN ('u', 'p')
  AND conrelid::regclass::text IN (
    'tally_companies',
    'balance_sheet_reports',
    'profit_loss_reports',
    'tally_ratio_analysis',
    'tally_cash_flow',
    'machine_companies',
    'ledger_bill_settlements',
    'user_companies',
    'script_control'
  )
ORDER BY 1, 2;

SELECT 'KEY_COLUMNS' AS section;
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    (table_name = 'balance_sheet_reports' AND column_name IN ('from_date','company_name','company_guid','as_on_date'))
    OR (table_name = 'profit_loss_reports' AND column_name IN ('from_date','to_date','company_name','company_guid'))
    OR (table_name = 'tally_companies' AND column_name IN ('guid','company_name','company_number','books_from'))
    OR (table_name = 'tally_ratio_analysis' AND column_name IN ('company_name','company_guid','from_date','to_date'))
    OR (table_name = 'tally_cash_flow' AND column_name IN ('company_name','company_guid','from_date','to_date'))
    OR (table_name = 'users' AND column_name IN ('is_super_admin','active_device_id','last_device_id','session_token'))
  )
ORDER BY table_name, column_name;
