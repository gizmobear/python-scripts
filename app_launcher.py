#!/usr/bin/env python3
"""
App Launcher & Cleanup

Usage:
  python app_launcher.py launch <app_name>   # launch app and record time
  python app_launcher.py task <app_name>     # if idle too long, securely delete cleanup_paths
  python app_launcher.py task-all            # run task for every app in config.json
  Note: <app_name> must match a key under "apps" in config.json.

Configuration:
  - Define apps in config.json (same directory).
  - Each app needs: cmd (list/string), max_days_idle (int), cleanup_paths (list of paths).

Author: Aaron Gruber
Change Log:
  - 2025-12-05: Refactored to use pathlib, logging, and sqlite3 for state management.
"""

import argparse
import datetime as dt
import json
import logging
import os
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, Iterable

# ---------------------------------------------------------------------------
# GLOBAL SETUP
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("AppLauncher")


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    """Load config.json once."""
    if not CONFIG_PATH.exists():
        logger.error(f"Missing config file at {CONFIG_PATH}.")
        logger.error("Create config.json (see example in this repo).")
        sys.exit(1)

    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse config.json: {e}")
        sys.exit(1)

    if not isinstance(data, dict) or "apps" not in data:
        logger.error("config.json must contain a JSON object with an 'apps' key.")
        sys.exit(1)

    return data


# ---------------------------------------------------------------------------
# PATH NORMALIZATION
# ---------------------------------------------------------------------------

def _normalize_path(value: Union[str, Path]) -> Path:
    """
    Convert config path values to Path, expanding ~ and env vars.
    We avoid strict resolve() so missing paths (to be deleted) don't error.
    """
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str):
        candidate = Path(os.path.expandvars(value)).expanduser()
    else:
        raise TypeError(f"Path value must be str or Path, got {type(value)}")

    try:
        return candidate.resolve(strict=False)
    except RuntimeError:
        # resolve can fail on deeply nested symlinks; fall back to raw
        return candidate


def _normalize_path_list(values: Iterable[Union[str, Path]]) -> List[Path]:
    return [_normalize_path(v) for v in values]


# ---------------------------------------------------------------------------
# STATE STORAGE (SQLite3)
# ---------------------------------------------------------------------------

def get_db_path() -> Path:
    """Return path to SQLite database."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home()
    
    state_dir = base / ".app_launch_tracker"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "state.db"


def init_db(db_path: Path) -> None:
    """Initialize the database schema if needed."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS launches (
                app_name TEXT PRIMARY KEY,
                last_launch_iso TEXT NOT NULL
            )
        """)
        conn.commit()


def record_launch(app_name: str) -> None:
    """Update the last launch time for the app."""
    db_path = get_db_path()
    init_db(db_path)
    
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                INSERT INTO launches (app_name, last_launch_iso)
                VALUES (?, ?)
                ON CONFLICT(app_name) DO UPDATE SET last_launch_iso = excluded.last_launch_iso
            """, (app_name, now))
            conn.commit()
    except sqlite3.Error as e:
        logger.warning(f"Failed to record launch in DB: {e}")


def get_last_launch(app_name: str) -> Optional[dt.datetime]:
    """Get the last launch time for the app."""
    db_path = get_db_path()
    init_db(db_path)
    
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("SELECT last_launch_iso FROM launches WHERE app_name = ?", (app_name,))
            row = cursor.fetchone()
            if row:
                return dt.datetime.fromisoformat(row[0])
    except (sqlite3.Error, ValueError) as e:
        logger.warning(f"Failed to read launch time: {e}")
    return None


# ---------------------------------------------------------------------------
# SECURE DELETE
# ---------------------------------------------------------------------------

def _make_writable(path: Path) -> None:
    """Ensure file/dir is writable."""
    try:
        # 0o700: user rwx
        if path.exists():
            path.chmod(0o777) 
    except Exception:
        pass

def secure_delete_file(path: Path, passes: int = 3) -> None:
    """Best-effort secure delete for a single file."""
    try:
        if not path.is_file():
            return

        _make_writable(path)
        length = path.stat().st_size

        with path.open("r+b", buffering=0) as f:
            for _ in range(passes):
                f.seek(0)
                # Overwrite with random bytes
                remaining = length
                chunk_size = 1024 * 1024
                while remaining > 0:
                    to_write = min(chunk_size, remaining)
                    f.write(secrets.token_bytes(to_write))
                    remaining -= to_write
                f.flush()
                # Force write to disk
                os.fsync(f.fileno())
        
        path.unlink()
    except Exception as e:
        logger.warning(f"Failed secure delete of file {path}: {e}")


def secure_delete_path(path: Union[str, Path], passes: int = 3) -> None:
    """Recursively secure delete a file or directory."""
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return

    # Handle symlinks (delete link, not target)
    if target.is_symlink():
        try:
            target.unlink()
        except Exception as e:
            logger.warning(f"Failed to remove symlink {target}: {e}")
        return

    if target.is_file():
        secure_delete_file(target, passes=passes)
        return

    # Directory: recursive delete
    # We walk using rglob('*') but that won't give us the order strictly bottom-up for dirs easily
    # So we use os.walk (or manual) to ensure we delete children before parents.
    # Actually, shutil.rmtree has no secure overwrite.
    # We'll stick to os.walk logic but with path objects for cleaner code.
    
    for root, dirs, files in os.walk(target, topdown=False):
        root_path = Path(root)
        for name in files:
            fpath = root_path / name
            if fpath.is_symlink():
                fpath.unlink(missing_ok=True)
            else:
                secure_delete_file(fpath, passes=passes)
        
        for name in dirs:
            dpath = root_path / name
            if dpath.is_symlink():
                dpath.unlink(missing_ok=True)
            else:
                _make_writable(dpath)
                try:
                    dpath.rmdir()
                except OSError as e:
                    logger.warning(f"Failed to remove dir {dpath}: {e}")

    # Finally remove top directory
    _make_writable(target)
    try:
        target.rmdir()
    except OSError as e:
        logger.warning(f"Failed to remove directory {target}: {e}")


def secure_delete_paths(paths: Iterable[Union[str, Path]], passes: int = 3) -> None:
    for p in paths:
        path_obj = _normalize_path(p)
        logger.info(f"Securely deleting: {path_obj}")
        secure_delete_path(path_obj, passes=passes)


# ---------------------------------------------------------------------------
# APP ACTIONS
# ---------------------------------------------------------------------------

def get_app_config(app_name: str) -> Dict[str, Any]:
    config = load_config()
    app = config.get("apps", {}).get(app_name)
    if not app:
        logger.error(f"Unknown app '{app_name}'. Configure it in config.json.")
        sys.exit(1)
    return app


def _normalize_cmd(cmd_value: Any) -> List[str]:
    """Ensure cmd is a list of strings."""
    if isinstance(cmd_value, list) and all(isinstance(part, str) for part in cmd_value):
        return cmd_value
    if isinstance(cmd_value, str):
        # posix=True usually handles quotes better, even on Windows for many cases,
        # but standard practice for Windows shell is posix=False.
        split_cmd = shlex.split(cmd_value, posix=(os.name != "nt"))
        if split_cmd:
            return split_cmd
    logger.error("'cmd' must be a non-empty list of strings or a string command line.")
    sys.exit(1)


def handle_launch(app_name: str) -> None:
    app = get_app_config(app_name)
    raw_cmd = app.get("cmd")
    if not raw_cmd:
        logger.error(f"No 'cmd' configured for app '{app_name}'.")
        sys.exit(1)

    cmd = _normalize_cmd(raw_cmd)
    
    # Resolve first argument (executable)
    exe = cmd[0]
    resolved_exe = shutil.which(exe) or (exe if Path(exe).exists() else None)
    
    if not resolved_exe:
        logger.error(f"Executable not found for '{app_name}': {exe}")
        logger.error("Check the path in config.json or ensure it is on PATH.")
        sys.exit(1)
    
    cmd[0] = resolved_exe

    logger.info(f"Launching '{app_name}': {cmd}")
    try:
        # Detach process
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            subprocess.Popen(cmd, creationflags=creationflags)
        else:
            subprocess.Popen(cmd, start_new_session=True)
    except OSError as e:
        logger.error(f"Failed to launch '{app_name}': {e}")
        sys.exit(1)

    record_launch(app_name)
    logger.info(f"Recorded launch time for '{app_name}'.")


def handle_task(app_name: str) -> None:
    app = get_app_config(app_name)
    max_days = app.get("max_days_idle")
    cleanup_paths_raw = app.get("cleanup_paths", [])

    if cleanup_paths_raw and not isinstance(cleanup_paths_raw, (list, tuple)):
        logger.error(f"'cleanup_paths' for '{app_name}' must be a list.")
        sys.exit(1)

    cleanup_paths = _normalize_path_list(cleanup_paths_raw)

    if max_days is None:
        logger.warning(f"No 'max_days_idle' configured for '{app_name}'. Nothing to do.")
        return

    last_launch = get_last_launch(app_name)
    now = dt.datetime.now(dt.timezone.utc)

    if last_launch is None:
        logger.info(f"App '{app_name}' has never been launched (no record).")
        # Optimization: If never launched, do we delete? 
        # Requirement was "if idle too long". Never launched = infinite idle?
        # Usually implies we shouldn't delete if we never used it, OR we should?
        # Let's assume safely: if record missing, treat as "unknown/infinite" but
        # usually we might want to check file timestamps.
        # For this script's contract: launch app -> record time.
        # If no record, we can't judge "idle from last usage".
        # Let's assume we skip unless we have data, to be safe.
        idle_days = None
    else:
        if last_launch.tzinfo is None:
            last_launch = last_launch.replace(tzinfo=dt.timezone.utc)
        
        delta = now - last_launch
        idle_days = delta.days
        logger.info(f"App '{app_name}' last launch: {last_launch.isoformat()} ({idle_days} days ago)")

    if idle_days is not None and idle_days > max_days:
        if not cleanup_paths:
            logger.info(f"No cleanup paths configured for '{app_name}'. Skipping deletion.")
            return
        logger.info(f"Idle threshold exceeded (>{max_days} days). Proceeding to secure delete.")
        secure_delete_paths(cleanup_paths, passes=3)
    else:
        logger.info(f"App '{app_name}' status: OK (Idle: {idle_days} vs Max: {max_days})")


def handle_task_all() -> None:
    """Run task for every configured app."""
    config = load_config()
    apps = config.get("apps", {})
    if not apps:
        logger.info("No apps configured in config.json.")
        return
    for app_name in apps:
        logger.info(f"Running task for '{app_name}'")
        handle_task(app_name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Launch apps and clean up their data if not used for N days."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_launch = subparsers.add_parser("launch", help="Launch an application and record launch time.")
    p_launch.add_argument("app_name", help="Name of the app as defined in config.json.")

    p_task = subparsers.add_parser("task", help="Check idle time and cleanup if needed.")
    p_task.add_argument("app_name", help="Name of the app as defined in config.json.")

    subparsers.add_parser("task-all", help="Check all apps.")

    args = parser.parse_args()

    if args.command == "launch":
        handle_launch(args.app_name)
    elif args.command == "task":
        handle_task(args.app_name)
    elif args.command == "task-all":
        handle_task_all()

if __name__ == "__main__":
    main()
