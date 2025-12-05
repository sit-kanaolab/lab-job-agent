#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from supabase import Client, create_client


HOME = Path.home()
LOG_ROOT = HOME / "lab_job_logs"
LOG_ROOT.mkdir(parents=True, exist_ok=True)
ALLOWED_SUFFIXES = {".py", ".ipynb"}
SKIP_DIRS = {".venv", ".cache", ".local", "anaconda3", "__pycache__", ".git"}
DEFAULT_JUPYTER_BASE_PORT = 8800
DEFAULT_SYNC_INTERVAL_MIN = 10
SYNC_STATE_FILE = LOG_ROOT / "last_sync.txt"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required in .env")
    return value


def load_config() -> Dict[str, Any]:
    load_dotenv()
    lab_email = require_env("LAB_EMAIL")
    return {
        "user": require_env("LAB_USER"),
        "user_id": os.getenv("LAB_USER_ID"),
        "email": lab_email,
        "supabase_url": require_env("SUPABASE_URL"),
        "supabase_service_key": require_env("SUPABASE_SERVICE_KEY"),
        "smtp_host": os.getenv("SMTP_HOST", "localhost"),
        "smtp_port": int(os.getenv("SMTP_PORT", "25")),
        "from_email": os.getenv("LAB_FROM_EMAIL", lab_email),
        "jupyter_base_port": int(os.getenv("JUPYTER_BASE_PORT", DEFAULT_JUPYTER_BASE_PORT)),
        "jupyter_ip": os.getenv("JUPYTER_IP", "0.0.0.0"),
        "jupyter_legacy": os.getenv("JUPYTER_LEGACY", "").lower() in {"1", "true", "yes"},
        "sync_interval_min": int(os.getenv("SYNC_INTERVAL_MIN", DEFAULT_SYNC_INTERVAL_MIN)),
    }


def make_supabase_client(config: Dict[str, Any]) -> Client:
    return create_client(config["supabase_url"], config["supabase_service_key"])


def resolve_user_id(client: Client, config: Dict[str, Any]) -> str:
    if config.get("user_id"):
        return config["user_id"]
    response = (
        client.table("users")
        .select("user_id")
        .eq("linux_user", config["user"])
        .limit(1)
        .execute()
    )
    data = getattr(response, "data", None) or []
    if not data:
        raise RuntimeError("Supabase users テーブルに linux_user の対応が見つかりません")
    user_id = data[0].get("user_id")
    if not user_id:
        raise RuntimeError("users.user_id が空です")
    return user_id


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def discover_scripts(user_id: str) -> List[Dict[str, Any]]:
    scripts: List[Dict[str, Any]] = []
    for pattern in ("*.py", "*.ipynb"):
        for path in HOME.rglob(pattern):
            if not path.is_file():
                continue
            if should_skip(path):
                continue
            try:
                rel = path.relative_to(HOME)
            except ValueError:
                continue
            scripts.append(
                {
                    "user_id": user_id,
                    "path": rel.as_posix(),
                    "type": path.suffix.lstrip("."),
                    "updated_at": now_utc_iso(),
                }
            )
    scripts.sort(key=lambda item: item["path"])
    return scripts


def sync_scripts(client: Client, user_id: str, scripts: List[Dict[str, Any]]) -> None:
    logging.info("Syncing %d script(s) for user_id=%s", len(scripts), user_id)
    client.table("scripts").delete().eq("user_id", user_id).execute()
    if scripts:
        client.table("scripts").insert(scripts).execute()


def should_sync_scripts(config: Dict[str, Any]) -> bool:
    interval_min = config.get("sync_interval_min", DEFAULT_SYNC_INTERVAL_MIN)
    if interval_min <= 0:
        return True
    if not SYNC_STATE_FILE.exists():
        return True
    try:
        last_text = SYNC_STATE_FILE.read_text().strip()
        last = datetime.fromisoformat(last_text)
    except Exception:
        return True
    now = datetime.now(timezone.utc)
    return now - last >= timedelta(minutes=interval_min)


def record_sync_time() -> None:
    try:
        SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SYNC_STATE_FILE.write_text(now_utc_iso())
    except Exception as exc:
        logging.warning("Failed to record sync time: %s", exc)


def fetch_next_job(client: Client, user_id: str) -> Optional[Dict[str, Any]]:
    response = (
        client.table("jobs")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    data = getattr(response, "data", None) or []
    return data[0] if data else None


def fetch_script(client: Client, script_id: Any, user_id: str) -> Optional[Dict[str, Any]]:
    # script_id カラムと id カラム両対応
    for key in ("script_id", "id"):
        try:
            response = (
                client.table("scripts")
                .select("*")
                .eq(key, script_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
        except Exception:
            continue
        data = getattr(response, "data", None) or []
        if data:
            return data[0]
    return None


def format_args(raw_args: Any) -> List[str]:
    if raw_args is None:
        return []
    if isinstance(raw_args, list):
        return [str(item) for item in raw_args]
    if isinstance(raw_args, dict):
        args: List[str] = []
        for key, value in raw_args.items():
            args.append(f"--{key}")
            if value is not None:
                args.append(str(value))
        return args
    if isinstance(raw_args, str):
        return shlex.split(raw_args)
    return [str(raw_args)]


def ensure_allowed_script(script_path: str, expected_type: str) -> Path:
    path = Path(script_path)
    if path.is_absolute():
        raise RuntimeError("script_path must be relative to the home directory")
    resolved = (HOME / path).resolve()
    try:
        resolved.relative_to(HOME)
    except ValueError:
        raise RuntimeError("script_path must stay under the home directory")
    if resolved.suffix not in ALLOWED_SUFFIXES:
        raise RuntimeError("Only .py or .ipynb scripts are allowed")
    if expected_type and resolved.suffix.lstrip(".") != expected_type:
        raise RuntimeError(f"Script type mismatch: expected {expected_type}")
    if not resolved.exists():
        raise RuntimeError(f"Script not found: {resolved}")
    if should_skip(resolved):
        raise RuntimeError(f"Script is under skipped directory: {resolved}")
    return resolved


def update_job(client: Client, job_id: Any, payload: Dict[str, Any], use_job_id: bool = False) -> None:
    columns = ["job_id", "id"] if use_job_id else ["id", "job_id"]
    last_exc: Optional[Exception] = None
    for column in columns:
        try:
            client.table("jobs").update(payload).eq(column, job_id).execute()
            return
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc


def get_tail(path: Path, num_lines: int = 20) -> Optional[str]:
    if not path.exists():
        return None
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return None
    if len(lines) <= num_lines:
        return "\n".join(lines)
    return "\n".join(lines[-num_lines:])


def send_email(
    config: Dict[str, Any],
    job: Dict[str, Any],
    status: str,
    retcode: Optional[int],
    stdout_path: Path,
    stderr_path: Path,
    error_message: Optional[str] = None,
) -> None:
    recipient = config.get("email")
    if not recipient:
        logging.info("LAB_EMAIL not set, skip email notification")
        return

    subject = f"[Lab job] {job.get('script_path') or job.get('script_id')} {status}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.get("from_email") or recipient
    msg["To"] = recipient

    body_lines = [
        f"job_id: {job.get('job_id') or job.get('id')}",
        f"script_id: {job.get('script_id') or job.get('id')}",
        f"script_path: {job.get('script_path')}",
        f"args: {json.dumps(job.get('args'), ensure_ascii=True)}",
        f"status: {status}",
        f"return code: {retcode}",
        f"stdout: {stdout_path}",
        f"stderr: {stderr_path}",
    ]
    if error_message:
        body_lines.append(f"error: {error_message}")

    msg.set_content("\n".join(body_lines))

    try:
        with smtplib.SMTP(config["smtp_host"], config["smtp_port"]) as server:
            server.send_message(msg)
        logging.info("Notification email sent to %s", recipient)
    except Exception as exc:
        logging.error("Failed to send email: %s", exc)


def build_nbconvert_command(script_full: Path, output_nb: Path) -> List[str]:
    return [
        "jupyter",
        "nbconvert",
        "--to",
        "notebook",
        "--execute",
        str(script_full),
        "--output",
        output_nb.name,
        "--output-dir",
        str(output_nb.parent),
    ]


def run_job(client: Client, config: Dict[str, Any], job: Dict[str, Any], user_id: str) -> None:
    job_id = job.get("job_id") or job.get("id")
    use_job_id = "job_id" in job
    script_id = job.get("script_id")
    args = format_args(job.get("args"))

    log_dir = LOG_ROOT / str(job_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"

    script_row = fetch_script(client, script_id, user_id) if script_id is not None else None
    script_path = (script_row.get("path") if script_row else None) or job.get("script_path") or ""
    if script_id is not None and not script_row:
        logging.error("Job %s rejected: script not found for id=%s", job_id, script_id)
        if job_id is not None:
            stderr_path.write_text("script not found\n")
            update_job(
                client,
                job_id,
                {
                    "status": "error",
                    "finished_at": now_utc_iso(),
                    "stderr_path": str(stderr_path),
                    "stdout_path": str(stdout_path),
                    "stderr_tail": "script not found",
                },
                use_job_id=use_job_id,
            )
            send_email(config, job, "error", None, stdout_path, stderr_path, "script not found")
        return
    if not script_path:
        stderr_path.write_text("script_path is empty\n")
        update_job(
            client,
            job_id,
            {
                "status": "error",
                "finished_at": now_utc_iso(),
                "stderr_path": str(stderr_path),
                "stdout_path": str(stdout_path),
                "stderr_tail": "script_path missing",
            },
            use_job_id=use_job_id,
        )
        send_email(config, job, "error", None, stdout_path, stderr_path, "script_path missing")
        return
    script_type = (script_row.get("type") if script_row else None) or Path(script_path).suffix.lstrip(".")
    job["script_path"] = script_path

    try:
        script_full = ensure_allowed_script(script_path, script_type)
    except Exception as exc:
        logging.error("Job %s rejected: %s", job_id, exc)
        update_job(
            client,
            job_id,
            {
                "status": "error",
                "finished_at": now_utc_iso(),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "retcode": None,
                "stderr_tail": str(exc),
            },
            use_job_id=use_job_id,
        )
        send_email(config, job, "error", None, stdout_path, stderr_path, str(exc))
        return

    update_job(
        client,
        job_id,
        {"status": "running", "started_at": now_utc_iso()},
        use_job_id=use_job_id,
    )

    if script_full.suffix == ".py":
        cmd = ["python", str(script_full)]
        if args:
            cmd.extend(args)
    else:
        output_nb = log_dir / "output.ipynb"
        cmd = build_nbconvert_command(script_full, output_nb)
        if args:
            with stderr_path.open("a") as stderr_f:
                stderr_f.write("Args are ignored for ipynb jobs.\n")

    logging.info("Running job %s: %s", job_id, " ".join(shlex.quote(part) for part in cmd))

    retcode: Optional[int] = None
    with stdout_path.open("w") as stdout_f, stderr_path.open("w") as stderr_f:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_f,
                stderr=stderr_f,
                cwd=script_full.parent,
                start_new_session=True,
            )
            retcode = proc.wait()
            logging.info("Job %s finished with %s", job_id, retcode)
        except Exception as exc:
            logging.error("Job %s failed to start: %s", job_id, exc)
            stderr_f.write(f"Failed to start job: {exc}\n")
            retcode = None

    status = "done" if retcode == 0 else "error"
    payload = {
        "status": status,
        "finished_at": now_utc_iso(),
        "retcode": retcode,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_tail": get_tail(stdout_path),
        "stderr_tail": get_tail(stderr_path),
    }
    update_job(client, job_id, payload, use_job_id=use_job_id)
    send_email(config, job, status, retcode, stdout_path, stderr_path)


def fetch_pending_session(client: Client, user_id: str) -> Optional[Dict[str, Any]]:
    response = (
        client.table("jupyter_sessions")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    data = getattr(response, "data", None) or []
    return data[0] if data else None


def update_session(
    client: Client, session_id: Any, payload: Dict[str, Any], use_session_id: bool = False
) -> None:
    columns = ["session_id", "id"] if use_session_id else ["id", "session_id"]
    last_exc: Optional[Exception] = None
    for column in columns:
        try:
            client.table("jupyter_sessions").update(payload).eq(column, session_id).execute()
            return
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc


def launch_jupyter(config: Dict[str, Any], session_id: Any, log_file: Path, port: int, token: str) -> subprocess.Popen:
    app_prefix = "NotebookApp" if config.get("jupyter_legacy") else "ServerApp"
    cmd = [
        "jupyter",
        "lab",
        "--no-browser",
        f"--port={port}",
        f"--ip={config['jupyter_ip']}",
        f"--{app_prefix}.token={token}",
        f"--{app_prefix}.password=''",
    ]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a") as log_f:
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=log_f,
            cwd=HOME,
            start_new_session=True,
        )
    return proc


def handle_jupyter_sessions(client: Client, config: Dict[str, Any], user_id: str) -> None:
    session = fetch_pending_session(client, user_id)
    if not session:
        return
    session_id = session.get("session_id") or session.get("id")
    use_session_id = "session_id" in session
    port_base = config["jupyter_base_port"]
    port = port_base + (os.getuid() % 100)
    token = secrets.token_hex(16)
    log_file = LOG_ROOT / "jupyter" / f"{session_id}.log"

    update_session(
        client,
        session_id,
        {"status": "starting", "updated_at": now_utc_iso()},
        use_session_id=use_session_id,
    )

    try:
        proc = launch_jupyter(config, session_id, log_file, port, token)
    except Exception as exc:
        logging.error("Failed to start Jupyter: %s", exc)
        update_session(
            client,
            session_id,
            {
                "status": "error",
                "error_message": str(exc),
                "updated_at": now_utc_iso(),
            },
            use_session_id=use_session_id,
        )
        return

    update_session(
        client,
        session_id,
        {
            "status": "running",
            "port": port,
            "token": token,
            "pid": proc.pid,
            "updated_at": now_utc_iso(),
        },
        use_session_id=use_session_id,
    )
    logging.info("Jupyter session %s running on port %s", session_id, port)


def main() -> int:
    configure_logging()
    try:
        config = load_config()
    except Exception as exc:
        logging.error("Failed to load configuration: %s", exc)
        return 1

    try:
        client = make_supabase_client(config)
    except Exception as exc:
        logging.error("Failed to create Supabase client: %s", exc)
        return 1

    try:
        user_id = resolve_user_id(client, config)
    except Exception as exc:
        logging.error("Failed to resolve user_id: %s", exc)
        return 1

    try:
        if should_sync_scripts(config):
            scripts = discover_scripts(user_id)
            sync_scripts(client, user_id, scripts)
            record_sync_time()
        else:
            logging.info(
                "Skip script sync (last sync within %s min)",
                config.get("sync_interval_min", DEFAULT_SYNC_INTERVAL_MIN),
            )
    except Exception as exc:
        logging.error("Failed to sync scripts: %s", exc)

    try:
        job = fetch_next_job(client, user_id)
    except Exception as exc:
        logging.error("Failed to fetch job: %s", exc)
        return 1

    if job:
        run_job(client, config, job, user_id)
    else:
        logging.info("No pending jobs for %s", config["user"])

    try:
        handle_jupyter_sessions(client, config, user_id)
    except Exception as exc:
        logging.error("Failed to handle Jupyter sessions: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
