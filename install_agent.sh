#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$HOME/lab_job_agent"
REPO_URL="https://github.com/sit-kanaolab/lab-job-agent.git"
ENV_FILE="$BASE_DIR/.env"

mkdir -p "$BASE_DIR"
if [ ! -d "$BASE_DIR/.git" ]; then
  git clone "$REPO_URL" "$BASE_DIR"
else
  git -C "$BASE_DIR" pull --ff-only
fi

python3 -m venv "$BASE_DIR/.venv"
"$BASE_DIR/.venv/bin/pip" install --upgrade pip
"$BASE_DIR/.venv/bin/pip" install -r "$BASE_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  read -rp "LAB_EMAIL: " LAB_EMAIL
  read -rp "LAB_USER_ID (Supabase Auth user.id, optional): " LAB_USER_ID
  read -rp "SUPABASE_URL: " SUPABASE_URL
  read -rp "SUPABASE_SERVICE_KEY: " SUPABASE_SERVICE_KEY
  cat >"$ENV_FILE" <<EOF
LAB_USER=$(whoami)
LAB_EMAIL=$LAB_EMAIL
LAB_USER_ID=$LAB_USER_ID
SUPABASE_URL=$SUPABASE_URL
SUPABASE_SERVICE_KEY=$SUPABASE_SERVICE_KEY
EOF
fi

CRON_LINE="* * * * * cd $BASE_DIR && $BASE_DIR/.venv/bin/python agent.py >> $BASE_DIR/agent.log 2>&1"
(
  crontab -l 2>/dev/null | grep -F -v "$BASE_DIR/.venv/bin/python agent.py" || true
  echo "$CRON_LINE"
) | crontab -
