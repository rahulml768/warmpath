-- Pending/unknown writes must not be reclaimed on a timer or by the same run.
create or replace function ledger_begin(p_action_id text, p_run_id text, p_kind text)
returns boolean language plpgsql as $$
declare claimed text;
begin
  insert into action_ledger(action_id, run_id, kind, status, attempts, updated)
  values(p_action_id, p_run_id, p_kind, 'pending', 1, now())
  on conflict(action_id) do update
    set status='pending', run_id=excluded.run_id, attempts=action_ledger.attempts+1, updated=now()
    where action_ledger.status='failed'
  returning action_id into claimed;
  return claimed is not null;
end $$;
