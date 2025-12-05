#!/usr/bin/env python3
"""
App Launcher & Cleanup

Usage:
  python app_launcher.py launch <app_name>   # launch app and record time
  python app_launcher.py task <app_name>     # if idle too long, securely delete cleanup_paths
  Note: <app_name> must match defined app name in config.json.

Configuration:
  - Define apps in config.json (same directory). See config.json for structure and examples.
  - Each app needs: cmd (list), max_days_idle (int), cleanup_paths (list of paths).

Author: Aaron Gruber
Change Log:
  - 2025-12-05: Added external config.json support, state file locking, and improved symlink deletion.
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import stat
import subprocess
import sys
import time
from typing import Dict, Any, List, Optional

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_CONFIG_CACHE: Optional[Dict[str, Any]] = None

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# Configuration now lives in config.json (same directory as this script).
# See config.json for example structure:
# {
#   "apps": {
#     "example_app": {
#       "cmd": ["C:\\\\Path\\\\To\\\\App.exe"],
#       "max_days_idle": 30,
#       "cleanup_paths": ["C:\\\\Path\\\\To\\\\App\\\\cache"]
#     }
#   }
# }
#
# WARNING: On SSDs, *no* software-only method can guarantee true secure deletion
# due to wear-leveling. This does best-effort overwrites + delete.


def load_config() -> Dict[str, Any]:
    """Load config.json once and reuse; exit with a helpful message on errors."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] Missing config file at {CONFIG_PATH}.", file=sys.stderr)
        print("        Create config.json (see example in this repo).", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse config.json: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("[ERROR] config.json must contain a JSON object.", file=sys.stderr)
        sys.exit(1)

    apps = data.get("apps")
    if not isinstance(apps, dict):
        print("[ERROR] config.json must define an 'apps' object mapping names to configs.", file=sys.stderr)
        sys.exit(1)

    _CONFIG_CACHE = data
    return data


# ---------------------------------------------------------------------------
# STATE STORAGE
# ---------------------------------------------------------------------------

def get_state_file() -> str:
    """Return path to JSON file storing last launch times."""
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.expanduser("~")

    state_dir = os.path.join(base, ".app_launch_tracker")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _read_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # Corrupt file? Start fresh.
        return {}


def _write_state(path: str, state: Dict[str, Any]) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)


def _state_lock_path() -> str:
    return get_state_file() + ".lock"


@contextlib.contextmanager
def _state_lock(timeout: float = 5.0, retry_delay: float = 0.1):
    """Cross-platform file lock to avoid concurrent state writes."""
    lock_path = _state_lock_path()
    if os.name == "nt":
        import msvcrt

        lock_file = open(lock_path, "a+b")
        start = time.time()
        try:
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() - start >= timeout:
                        raise TimeoutError("Timed out waiting for state lock")
                    time.sleep(retry_delay)
            yield
        finally:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            lock_file.close()
    else:
        import fcntl

        lock_file = open(lock_path, "a+b")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_file.close()


def load_state() -> Dict[str, Any]:
    path = get_state_file()
    try:
        with _state_lock():
            return _read_state(path)
    except TimeoutError as e:
        print(f"[WARN] Could not acquire state lock for read: {e}", file=sys.stderr)
        return {}


def save_state(state: Dict[str, Any]) -> None:
    path = get_state_file()
    try:
        with _state_lock():
            _write_state(path, state)
    except TimeoutError as e:
        print(f"[WARN] Could not acquire state lock for write: {e}", file=sys.stderr)


def record_launch(app_name: str) -> None:
    state_path = get_state_file()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        with _state_lock():
            state = _read_state(state_path)
            state.setdefault("apps", {})
            state["apps"][app_name] = {"last_launch": now}
            _write_state(state_path, state)
    except TimeoutError as e:
        print(f"[WARN] Could not record launch due to lock timeout: {e}", file=sys.stderr)


def get_last_launch(app_name: str):
    state = load_state()
    app_info = state.get("apps", {}).get(app_name)
    if not app_info:
        return None
    try:
        return dt.datetime.fromisoformat(app_info["last_launch"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SECURE DELETE
# ---------------------------------------------------------------------------

def _make_writable(path: str) -> None:
    """Ensure file/dir is writable so we can overwrite/delete."""
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IWUSR)
    except Exception:
        pass


def secure_delete_file(path: str, passes: int = 3) -> None:
    """Best-effort secure delete for a single file."""
    try:
        if not os.path.isfile(path):
            return

        _make_writable(path)
        length = os.path.getsize(path)

        with open(path, "r+b", buffering=0) as f:
            for _ in range(passes):
                f.seek(0)
                # overwrite with random bytes
                remaining = length
                chunk_size = 1024 * 1024
                while remaining > 0:
                    to_write = min(chunk_size, remaining)
                    f.write(os.urandom(to_write))
                    remaining -= to_write
                f.flush()
                os.fsync(f.fileno())
        # Finally delete the file
        os.remove(path)
    except Exception as e:
        print(f"[WARN] Failed secure delete of file {path}: {e}", file=sys.stderr)


def secure_delete_path(path: str, passes: int = 3) -> None:
    """Recursively secure delete a file or directory."""
    if not os.path.lexists(path):
        return

    if os.path.islink(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"[WARN] Failed to remove symlink {path}: {e}", file=sys.stderr)
        return

    if os.path.isfile(path):
        secure_delete_file(path, passes=passes)
        return

    # Directory: walk bottom-up
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            file_path = os.path.join(root, name)
            if os.path.islink(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"[WARN] Failed to remove symlink {file_path}: {e}", file=sys.stderr)
                continue
            secure_delete_file(file_path, passes=passes)
        for name in dirs:
            dir_path = os.path.join(root, name)
            if os.path.islink(dir_path):
                try:
                    os.remove(dir_path)
                except Exception as e:
                    print(f"[WARN] Failed to remove symlink {dir_path}: {e}", file=sys.stderr)
                continue
            try:
                _make_writable(dir_path)
                os.rmdir(dir_path)
            except Exception:
                pass
    # Remove the top-level directory if empty
    try:
        _make_writable(path)
        os.rmdir(path)
    except Exception as e:
        print(f"[WARN] Failed to remove directory {path}: {e}", file=sys.stderr)


def secure_delete_paths(paths: List[str], passes: int = 3) -> None:
    for p in paths:
        print(f"[INFO] Securely deleting: {p}")
        secure_delete_path(p, passes=passes)


# ---------------------------------------------------------------------------
# APP ACTIONS
# ---------------------------------------------------------------------------

def get_app_config(app_name: str) -> Dict[str, Any]:
    config = load_config()
    app = config.get("apps", {}).get(app_name)
    if not app:
        print(f"[ERROR] Unknown app '{app_name}'. Configure it in config.json.", file=sys.stderr)
        sys.exit(1)
    return app


def handle_launch(app_name: str) -> None:
    app = get_app_config(app_name)
    cmd = app.get("cmd")
    if not cmd:
        print(f"[ERROR] No 'cmd' configured for app '{app_name}'.", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Launching '{app_name}': {cmd}")
    try:
        # Start process detached from this script
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS (0x00000002 | 0x00000008)
            creationflags = 0x00000002 | 0x00000008
            subprocess.Popen(cmd, creationflags=creationflags)
        else:
            subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError:
        print(f"[ERROR] Executable not found for '{app_name}'. Check 'cmd' path.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Failed to launch '{app_name}': {e}", file=sys.stderr)
        sys.exit(1)

    record_launch(app_name)
    print(f"[INFO] Recorded launch time for '{app_name}'.")


def handle_task(app_name: str) -> None:
    app = get_app_config(app_name)
    max_days = app.get("max_days_idle")
    cleanup_paths = app.get("cleanup_paths", [])

    if max_days is None:
        print(f"[WARN] No 'max_days_idle' configured for '{app_name}'. Nothing to do.")
        return

    last_launch = get_last_launch(app_name)
    now = dt.datetime.now(dt.timezone.utc)

    if last_launch is None:
        print(f"[INFO] App '{app_name}' has never been launched (no record).")
        idle_days = None
    else:
        if last_launch.tzinfo is None:
            # assume UTC if naive
            last_launch = last_launch.replace(tzinfo=dt.timezone.utc)
        delta = now - last_launch
        idle_days = delta.days
        print(f"[INFO] App '{app_name}' last launch: {last_launch.isoformat()} "
              f"({idle_days} days ago)")

    if idle_days is None or idle_days > max_days:
        if not cleanup_paths:
            print(f"[INFO] No cleanup paths configured for '{app_name}'. Skipping deletion.")
            return
        print(f"[INFO] Idle threshold exceeded (>{max_days} days). Proceeding to secure delete.")
        secure_delete_paths(cleanup_paths, passes=3)
    else:
        print(f"[INFO] App '{app_name}' not idle long enough (<= {max_days} days). No action.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Launch apps and clean up their data if not used for N days."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_launch = subparsers.add_parser("launch", help="Launch an application and record launch time.")
    p_launch.add_argument("app_name", help="Name of the app as defined in APPS.")

    p_task = subparsers.add_parser(
        "task",
        help="Check last launch; if idle too long, securely delete configured paths.",
    )
    p_task.add_argument("app_name", help="Name of the app as defined in APPS.")

    return parser.parse_args()


def main():
    args = parse_args()

    if args.command == "launch":
        handle_launch(args.app_name)
    elif args.command == "task":
        handle_task(args.app_name)
    else:
        # Should never hit this with argparse's required=True
        print("[ERROR] Unknown command.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
