-- WarmPath schema. Run once in the Supabase SQL editor of a NEW project (never a product database).
--
-- Every table here is written by the server with the service role key. Row Level Security is on
-- with no policies, so the anon key reads nothing - a leaked browser key exposes no lead data.

-- ── leads: one row per person the team is working ────────────────────────────
create table if not exists leads (
  key              text primary key,          -- verified email when we have one, else the comment id
  email            text,
  name             text,
  company          text,
  domain           text,
  comment          text,
  intent           text,
  confidence       double precision,
  warm             boolean default false,
  knows            text,                       -- who we already know there (name or address)
  stage            text,                       -- human-readable stage shown in the Leads page
  status           text,                       -- awaiting_reply | meeting_booked | ...
  subject          text,
  sent_at          timestamptz,
  offered          jsonb default '{}'::jsonb,  -- slots offered to them, with checked flags
  pending_booking  text default '',            -- an agreed time not yet on the calendar
  meeting          text,
  comment_id       text,
  lead_run         text,
  data             jsonb default '{}'::jsonb,  -- anything else a run recorded
  updated_at       timestamptz default now()
);
create index if not exists leads_status_idx on leads (status);

-- ── runs: the full trace of every execution, refusals included ──────────────
create table if not exists runs (
  run_id       text primary key,
  kind         text not null,                  -- lead | reply
  subject      text,
  message_id   text,
  mode         text,                           -- dry_run | live
  state        text,
  states       jsonb,
  steps        jsonb,
  result       text,
  reason_code  text,
  evidence     jsonb,
  started      timestamptz,
  is_eval      boolean default false,
  violations   integer default 0,
  created_at   timestamptz default now()
);
create index if not exists runs_started_idx on runs (started desc);

-- ── action ledger: every external write, at most once ────────────────────────
create table if not exists action_ledger (
  action_id  text primary key,                 -- send:<person> | reply:<comment> | meeting:<person>
  run_id     text,
  kind       text,
  status     text,                             -- pending | done | failed
  attempts   integer default 0,
  detail     text,
  updated    timestamptz default now()
);

-- The check-and-claim is one statement, so two workers can never both be told "go ahead".
create or replace function ledger_begin(p_action_id text, p_run_id text, p_kind text)
returns boolean language plpgsql as $$
declare current_status text;
begin
  select status into current_status from action_ledger where action_id = p_action_id for update;
  if current_status = 'done' or current_status = 'pending' and p_run_id is distinct from
     (select run_id from action_ledger where action_id = p_action_id) and
     (select updated from action_ledger where action_id = p_action_id) > now() - interval '10 minutes' then
    update action_ledger set attempts = attempts + 1, updated = now() where action_id = p_action_id;
    return false;
  end if;
  insert into action_ledger (action_id, run_id, kind, status, attempts, updated)
  values (p_action_id, p_run_id, p_kind, 'pending', 1, now())
  on conflict (action_id) do update set attempts = action_ledger.attempts + 1, status = 'pending',
    run_id = excluded.run_id, updated = now();
  return true;
end $$;

-- ── chat with Alex ──────────────────────────────────────────────────────────
create table if not exists chat_messages (
  id       bigint generated always as identity primary key,
  ts       timestamptz default now(),
  author   text,
  kind     text,
  text     text,
  payload  jsonb default '{}'::jsonb
);

create table if not exists approvals (
  run_id    text primary key,
  decision  text,                              -- approved | rejected | review
  ts        timestamptz default now()
);

-- ── autopilot ───────────────────────────────────────────────────────────────
create table if not exists claims (
  key      text primary key,                   -- comment:<id> | reply:<message-id> | resume:...
  kind     text,
  status   text,                               -- in_progress | done | failed
  updated  timestamptz default now()
);

create table if not exists watches (
  source  text primary key,                    -- a LinkedIn post URL, or "demo"
  label   text,
  added   timestamptz default now(),
  active  boolean default true
);

create table if not exists settings (
  key    text primary key,
  value  text
);

-- ── reliability: issues found by the auditor, and evaluation results ─────────
create table if not exists audit_violations (
  id          bigint generated always as identity primary key,
  run_id      text,
  agent       text,
  invariant   text,
  instruction text,
  detail      text,
  created_at  timestamptz default now(),
  unique (run_id, invariant, detail)
);

create table if not exists eval_results (
  id          bigint generated always as identity primary key,
  created_at  timestamptz default now(),
  runs_per_comment integer,
  scenario_pass    integer,
  scenario_total   integer,
  audit_violations integer,
  report      jsonb
);

-- ── lock it down ────────────────────────────────────────────────────────────
alter table leads            enable row level security;
alter table runs             enable row level security;
alter table action_ledger    enable row level security;
alter table chat_messages    enable row level security;
alter table approvals        enable row level security;
alter table claims           enable row level security;
alter table watches          enable row level security;
alter table settings         enable row level security;
alter table audit_violations enable row level security;
alter table eval_results     enable row level security;
