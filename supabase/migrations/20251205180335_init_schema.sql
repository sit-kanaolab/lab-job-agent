-- 初期スキーマ
begin;

-- 拡張
create extension if not exists "pgcrypto";

-- users: Supabase Auth の user.id を user_id として使う
create table if not exists public.users (
  user_id    uuid primary key,
  linux_user text not null unique,
  email      text not null
);

-- scripts: Home 配下の *.py / *.ipynb 一覧
create table if not exists public.scripts (
  script_id  bigserial primary key,
  user_id    uuid not null references public.users(user_id) on delete cascade,
  path       text not null,
  type       text not null check (type in ('py', 'ipynb')),
  updated_at timestamptz not null default now(),
  constraint scripts_user_path_unique unique (user_id, path)
);

-- jobs: 実行キュー
create table if not exists public.jobs (
  job_id       uuid primary key default gen_random_uuid(),
  user_id      uuid not null references public.users(user_id) on delete cascade,
  script_id    bigint references public.scripts(script_id),
  script_path  text,
  args         text,
  status       text not null check (status in ('pending', 'running', 'done', 'error')),
  stdout_path  text,
  stderr_path  text,
  retcode      int,
  stdout_tail  text,
  stderr_tail  text,
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz,
  constraint jobs_script_presence check (script_id is not null or script_path is not null)
);

-- jupyter_sessions: Jupyter 起動リクエスト
create table if not exists public.jupyter_sessions (
  session_id    uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users(user_id) on delete cascade,
  status        text not null check (status in ('pending', 'starting', 'running', 'error', 'stopped')),
  port          int,
  token         text,
  pid           int,
  error_message text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- RLS 有効化
alter table public.users            enable row level security;
alter table public.scripts          enable row level security;
alter table public.jobs             enable row level security;
alter table public.jupyter_sessions enable row level security;

-- RLS ポリシー
create policy "Users can manage self" on public.users
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy "Scripts by owner" on public.scripts
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy "Jobs by owner" on public.jobs
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy "Sessions by owner" on public.jupyter_sessions
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- インデックス
create index if not exists idx_scripts_user on public.scripts(user_id);
create index if not exists idx_jobs_user_status_created on public.jobs(user_id, status, created_at);
create index if not exists idx_sessions_user_status_created on public.jupyter_sessions(user_id, status, created_at);

-- updated_at 自動更新
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_jupyter_sessions_set_updated_at on public.jupyter_sessions;
create trigger trg_jupyter_sessions_set_updated_at
before update on public.jupyter_sessions
for each row execute function public.set_updated_at();

commit;
