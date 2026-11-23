-- Autotrade module schema (PostgreSQL 13+). Idempotent: safe to run more than once.
-- The service connects with its own database role. These tables are NOT meant to be reachable from
-- a browser/PostgREST client: RLS is enabled with no policies and API roles lose all privileges.

create table if not exists autotrade_credentials (
  user_id         text        not null,                       -- your internal account id
  kind            text        not null check (kind in ('demo','real')),
  ciphertext      bytea       not null,                       -- AES-256-GCM blob: format | key version | nonce | ct+tag
  key_version     smallint    not null,                       -- which vault key encrypted it (rotation)
  status          text        not null default 'active' check (status in ('active','expired')),
  captured_at     timestamptz not null default now(),
  last_checked_at timestamptz,
  last_error      text,
  meta            jsonb       not null default '{}'::jsonb,   -- non-secret refresh metadata (capture method, derived flag)
  primary key (user_id, kind)
);
create index if not exists autotrade_credentials_recheck
  on autotrade_credentials (last_checked_at nulls first) where status = 'active';

create table if not exists autotrade_settings (
  user_id    text        primary key,
  settings   jsonb       not null,                            -- TradingSettings (validated in code)
  updated_at timestamptz not null default now()
);
-- Stake strategy keys inside settings (Phase 3): stake_mode 'fixed' | 'martingale' (default 'fixed'),
-- martingale_multiplier (default 2.0), martingale_max_steps (default 3), base_stake (null = use stake).
-- Older rows without these keys read as the defaults; the checks below only reject invalid values.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'autotrade_settings_stake_mode') then
    alter table autotrade_settings add constraint autotrade_settings_stake_mode
      check (coalesce(settings->>'stake_mode', 'fixed') in ('fixed', 'martingale'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'autotrade_settings_martingale') then
    alter table autotrade_settings add constraint autotrade_settings_martingale
      check (coalesce((settings->>'martingale_multiplier')::numeric, 2.0) between 1.1 and 5
             and coalesce((settings->>'martingale_max_steps')::int, 3) between 1 and 6
             and (settings->>'base_stake' is null or (settings->>'base_stake')::numeric >= 1));
  end if;
end $$;

create table if not exists autotrade_trades (
  id              bigserial   primary key,
  user_id         text        not null,
  client_ref      text        not null,                       -- our idempotency key, written BEFORE the order is sent
  mode            text        not null check (mode in ('demo','live')),
  asset           text        not null,
  direction       text        not null check (direction in ('CALL','PUT')),
  stake           numeric(12,2) not null,
  expiry_seconds  integer     not null,
  status          text        not null default 'pending'
                  check (status in ('pending','open','settled','failed','unknown')),
  broker_trade_id text,
  payout          smallint,                                   -- payout % read from the feed at order time
  outcome         text        check (outcome in ('win','loss','draw')),
  profit          numeric(12,2),
  reason          text        not null default '',
  meta            jsonb       not null default '{}'::jsonb,
  error           text,
  opened_at       timestamptz not null default now(),
  settled_at      timestamptz,
  unique (user_id, client_ref)
);
create index if not exists autotrade_trades_user_opened on autotrade_trades (user_id, opened_at desc);
create index if not exists autotrade_trades_pnl on autotrade_trades (user_id, mode, settled_at) where status = 'settled';
create index if not exists autotrade_trades_open on autotrade_trades (user_id) where status in ('pending','open');

create table if not exists autotrade_bot_state (
  user_id         text        primary key,
  desired_running boolean     not null default false,         -- resume after a service restart
  status          text        not null,
  last_error      text,
  updated_at      timestamptz not null default now()
);

-- Last known account balance per account type, so it survives restarts (/at_balance).
create table if not exists autotrade_balances (
  user_id    text          not null,
  kind       text          not null check (kind in ('demo','real')),
  balance    numeric(14,2) not null,
  updated_at timestamptz   not null default now(),
  primary key (user_id, kind)
);

create table if not exists autotrade_audit (
  id         bigserial   primary key,
  user_id    text,
  event      text        not null,
  detail     jsonb       not null default '{}'::jsonb,        -- never contains tokens
  created_at timestamptz not null default now()
);
create index if not exists autotrade_audit_user on autotrade_audit (user_id, created_at desc);

-- Lock the tables down for API roles (no-ops where those roles do not exist).
alter table autotrade_credentials enable row level security;
alter table autotrade_settings    enable row level security;
alter table autotrade_trades      enable row level security;
alter table autotrade_bot_state   enable row level security;
alter table autotrade_audit       enable row level security;
alter table autotrade_balances    enable row level security;
do $$
declare r text;
begin
  foreach r in array array['anon','authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on autotrade_credentials, autotrade_settings, autotrade_trades, autotrade_bot_state, autotrade_audit, autotrade_balances from %I', r);
    end if;
  end loop;
end $$;
