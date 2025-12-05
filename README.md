# lab-job-agent

研究室サーバ上でユーザごとに常駐させ、Home 配下の `*.py` / `*.ipynb` を Supabase 経由でキュー実行・Jupyter 起動するエージェント。

## Supabase の最低限スキーマ
- `users(user_id uuid PK, linux_user text, email text)`
- `scripts(script_id bigserial PK, user_id uuid FK, path text, type text, updated_at timestamptz)`
- `jobs(job_id uuid PK default gen_random_uuid(), user_id uuid FK, script_id bigint FK, args text/jsonb, status text, stdout_path text, stderr_path text, retcode int, stdout_tail text, stderr_tail text, created_at timestamptz default now(), started_at timestamptz, finished_at timestamptz)`
- `jupyter_sessions(session_id uuid PK, user_id uuid FK, status text, port int, token text, pid int, error_message text, created_at timestamptz, updated_at timestamptz)`
- RLS は `user_id` で絞る想定。

## インストール（各 Linux ユーザ一回だけ）
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sit-kanaolab/lab-job-agent/main/install_agent.sh)
```
- `~/lab_job_agent` に clone/pull、venv セットアップ、`.env` が無ければ対話作成。
- cron へ `* * * * * cd $HOME/lab_job_agent && $HOME/lab_job_agent/.venv/bin/python agent.py >> $HOME/lab_job_agent/agent.log 2>&1` を追加。

## .env のキー
- `LAB_USER`：Linux ユーザ名（自動）
- `LAB_EMAIL`：通知先メール
- `LAB_USER_ID`：Supabase Auth の user.id（未設定なら `users` テーブルを linux_user で検索）
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`
- 任意：`SMTP_HOST`（既定 `localhost`）、`SMTP_PORT`（`25`）、`LAB_FROM_EMAIL`、`JUPYTER_BASE_PORT`（既定 8800）、`JUPYTER_IP`（既定 `0.0.0.0`）、`JUPYTER_LEGACY`（古い NotebookApp 系なら `true` に）、`SYNC_INTERVAL_MIN`（スクリプト同期間隔、既定 10 分）

## エージェントの動き
- スクリプト同期：`~` 以下を再帰で探索し `*.py` / `*.ipynb` を収集（`.venv`, `.cache`, `.local`, `anaconda3`, `__pycache__`, `.git` は除外）。`SYNC_INTERVAL_MIN` 間隔でのみ実行し、`scripts` を user_id 単位で全削除→再登録。
- ジョブ実行：`jobs` から `status='pending'` を1件取得→`running` に更新→`.py` は `python <path> [args]`、`.ipynb` は `jupyter nbconvert --execute`（args は無視）。ログは `~/lab_job_logs/<job_id>/stdout.log|stderr.log` に保存し、パス・retcode・tail を更新してメール通知。
- Jupyter 起動：`jupyter_sessions` の `pending` を拾い、ポートを決定（`JUPYTER_BASE_PORT` + uid%100）、トークン発行のうえ `jupyter lab --no-browser --ip=0.0.0.0 --port=... --ServerApp.token=... --ServerApp.password=''`（古い環境なら NotebookApp）をデタッチ起動。`running` に port/token/pid を保存。
