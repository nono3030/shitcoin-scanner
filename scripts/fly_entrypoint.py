#!/usr/bin/env python3
"""
Fly / container entrypoint:
  1) Ensure DATA_ROOT dirs
  2) Start dashboard HTTP (health + UI) in a daemon thread
  3) Run daily scheduler in the main thread (blocks forever)

Keeps one machine + one volume for both web and post-close trading job.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))
os.environ.setdefault("DATA_ROOT", str(DATA_ROOT))
os.environ.setdefault("BIND_HOST", "0.0.0.0")
os.environ.setdefault("PORT", "8080")
os.environ.setdefault("DAILY_RUN_UTC_HOUR", "0")
os.environ.setdefault("DAILY_RUN_UTC_MINUTE", "15")


def _ensure_dirs() -> None:
    for sub in ("live", "cache", "out", "paper"):
        (DATA_ROOT / sub).mkdir(parents=True, exist_ok=True)


def _start_dashboard() -> None:
    host = os.environ.get("BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    print(f"[entrypoint] starting dashboard on {host}:{port}", flush=True)
    from dashboard import serve

    serve(open_browser=False, host=host, port=port)


def _maybe_catch_up() -> None:
    """One-shot rattrapage after a pending deadlock. Stamp lives on the volume."""
    flag = os.environ.get("CATCH_UP_ON_BOOT", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return
    stamp = DATA_ROOT / "out" / "catchup_once.stamp"
    if stamp.exists():
        print(f"[entrypoint] catch-up already stamped ({stamp})", flush=True)
        return
    stamp.parent.mkdir(parents=True, exist_ok=True)
    print("[entrypoint] CATCH_UP_ON_BOOT → run_daily --catch-up --force-trade", flush=True)
    cmd = [
        sys.executable,
        str(APP_ROOT / "run_daily.py"),
        "--no-refresh",
        "--force-trade",
        "--catch-up",
        "--skip-dashboard",
    ]
    env = os.environ.copy()
    env["DATA_ROOT"] = str(DATA_ROOT)
    env["PYTHONPATH"] = str(APP_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(cmd, cwd=str(APP_ROOT), env=env, check=False)
        code = int(proc.returncode)
    except Exception as e:
        print(f"[entrypoint] catch-up FAILED spawn: {e}", flush=True)
        code = 1
    stamp.write_text(
        f"{datetime.now(timezone.utc).isoformat()} exit={code}\n",
        encoding="utf-8",
    )
    print(f"[entrypoint] catch-up done exit={code} stamped {stamp}", flush=True)


def main() -> int:
    os.chdir(APP_ROOT)
    _ensure_dirs()
    print(
        f"[entrypoint] APP_ROOT={APP_ROOT} DATA_ROOT={DATA_ROOT} "
        f"daily={os.environ.get('DAILY_RUN_UTC_HOUR')}:{os.environ.get('DAILY_RUN_UTC_MINUTE')}Z",
        flush=True,
    )

    t = threading.Thread(target=_start_dashboard, name="dashboard", daemon=True)
    t.start()
    # give health check a moment to bind
    time.sleep(1.5)
    # one-shot rattrapage (expire stuck pending + 4 market shorts) — does not block health
    try:
        _maybe_catch_up()
    except Exception as e:
        print(f"[entrypoint] catch-up warn: {e}", flush=True)

    import importlib.util

    sched_path = APP_ROOT / "scripts" / "daily_scheduler.py"
    spec = importlib.util.spec_from_file_location("daily_scheduler", sched_path)
    if spec is None or spec.loader is None:
        print("[entrypoint] cannot load daily_scheduler.py", flush=True)
        return 1
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return int(mod.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
