#!/usr/bin/env python3
"""
App Launcher & Automatic Cleanup Tool

DESCRIPTION:
    This application provides automated application launching with intelligent cleanup
    capabilities. It tracks when applications are launched and automatically removes
    application data (profiles, cache, etc.) if an app hasn't been used for a specified
    number of days. This is particularly useful for managing browser profiles and other
    applications that accumulate data over time.

    Features:
    - Launch applications and track usage timestamps
    - Automatic cleanup of idle application data
    - Secure deletion with multiple overwrite passes
    - Cross-platform support (Windows, Linux, macOS)
    - Comprehensive logging and error handling
    - Database-backed state management with schema versioning

USAGE:
    Launch an application:
        python app_launcher.py launch <app_name>
        Example: python app_launcher.py launch firefox

    Check idle status and cleanup if needed (single app):
        python app_launcher.py task <app_name>
        Example: python app_launcher.py task firefox

    Check idle status and cleanup for all configured apps:
        python app_launcher.py task-all

    Note: <app_name> must match a key under "apps" in config.json.

CONFIGURATION:
    Configuration is stored in config.json (same directory as this script).

    Required fields for each app:
    - cmd: Command to launch the app (string or list of strings)
    - max_days_idle: Positive integer - number of days idle before cleanup
    - cleanup_paths: List of file/directory paths to delete when idle threshold is met

    Example config.json:
    {
      "apps": {
        "firefox": {
          "cmd": ["C:/Program Files/Mozilla Firefox/firefox.exe"],
          "max_days_idle": 3,
          "cleanup_paths": [
            "C:/Users/Username/AppData/Roaming/Mozilla",
            "C:/Users/Username/AppData/Local/Mozilla"
          ]
        }
      }
    }

    Paths support:
    - Environment variables: %APPDATA%, $HOME, etc.
    - Home directory expansion: ~/path/to/file
    - Absolute and relative paths

LOGGING:
    All operations are logged to a file in the user's home directory:
    - Windows: C:\\Users\\<username>\\app_launcher.log
    - Unix/Linux: /home/<username>/app_launcher.log

    Log files are automatically rotated when they reach 10MB, keeping 5 backup files.
    Logs include timestamps, log levels, and full error tracebacks.

    The application runs silently when executed as a scheduled task, with all output
    written to the log file. When run from a terminal, logs also appear on the console.

DATABASE:
    Launch timestamps are stored in an SQLite database:
    - Windows: %APPDATA%\\.app_launch_tracker\\state.db
    - Unix/Linux: ~/.app_launch_tracker/state.db

    The database schema is versioned and automatically migrated when needed.

SECURE DELETION:
    When cleanup is triggered, files and directories are securely deleted using:
    - Multiple overwrite passes (default: 3) with random data
    - Files are overwritten before deletion to prevent recovery
    - Symlinks are handled safely (link removed, not target)
    - Errors during deletion are logged but don't stop processing

ERROR HANDLING:
    - Missing executables are detected and logged with warnings
    - Invalid configurations are validated at startup
    - Individual app failures don't stop batch processing (task-all)
    - All errors are logged with full tracebacks for debugging

AUTHOR: Aaron Gruber

CHANGE LOG:
    - 2025-12-05: Refactored to use pathlib, logging, and sqlite3 for state management.
    - 2025-01-XX: Added comprehensive logging, database versioning, config validation,
                   improved error handling, and executable existence checks.
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
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, Iterable

# ---------------------------------------------------------------------------
# GLOBAL SETUP
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


def setup_logging() -> None:
    """
    Configure logging to both file and console.
    
    Log file is created in the user's home directory:
    - Windows: C:\Users\<username>\app_launcher.log
    - Unix: /home/<username>/app_launcher.log
    
    Logs are rotated when they reach 10MB, keeping 5 backup files.
    """
    # Get user home directory
    if os.name == "nt":
        home_dir = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home_dir = Path.home()
    
    log_file = home_dir / "app_launcher.log"
    
    # Create formatter
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Setup file handler with rotation (10MB per file, keep 5 backups)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    # Setup console handler (for when run from terminal)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        fmt="[%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    ))
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # Add our handlers
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # Prevent duplicate logs from other libraries
    logging.getLogger("sqlite3").setLevel(logging.WARNING)


# Initialize logging
setup_logging()
logger = logging.getLogger("AppLauncher")


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

def validate_app_config(app_name: str, app_config: Dict[str, Any]) -> None:
    """
    Validate that an app configuration has all required fields and valid values.
    
    Args:
        app_name: Name of the app being validated
        app_config: Dictionary containing app configuration
        
    Raises:
        SystemExit: If validation fails
    """
    if not isinstance(app_config, dict):
        logger.error(f"App '{app_name}' configuration must be a JSON object.")
        sys.exit(1)
    
    # Validate 'cmd' field
    if "cmd" not in app_config:
        logger.error(f"App '{app_name}' missing required field 'cmd'.")
        sys.exit(1)
    
    cmd = app_config["cmd"]
    if not isinstance(cmd, (str, list)):
        logger.error(f"App '{app_name}': 'cmd' must be a string or list of strings.")
        sys.exit(1)
    
    if isinstance(cmd, list) and not cmd:
        logger.error(f"App '{app_name}': 'cmd' list cannot be empty.")
        sys.exit(1)
    
    if isinstance(cmd, str) and not cmd.strip():
        logger.error(f"App '{app_name}': 'cmd' string cannot be empty.")
        sys.exit(1)
    
    # Validate 'max_days_idle' if present
    if "max_days_idle" in app_config:
        max_days = app_config["max_days_idle"]
        if not isinstance(max_days, int) or max_days <= 0:
            logger.error(f"App '{app_name}': 'max_days_idle' must be a positive integer, got: {max_days}")
            sys.exit(1)
    
    # Validate 'cleanup_paths' if present
    if "cleanup_paths" in app_config:
        cleanup_paths = app_config["cleanup_paths"]
        if not isinstance(cleanup_paths, (list, tuple)):
            logger.error(f"App '{app_name}': 'cleanup_paths' must be a list or tuple.")
            sys.exit(1)
        
        if not all(isinstance(p, (str, Path)) for p in cleanup_paths):
            logger.error(f"App '{app_name}': All items in 'cleanup_paths' must be strings or Path objects.")
            sys.exit(1)


def load_config() -> Dict[str, Any]:
    """
    Load and validate config.json.
    
    Returns:
        Dictionary containing the configuration with validated 'apps' key.
        
    Raises:
        SystemExit: If config file is missing, invalid JSON, or missing 'apps' key.
    """
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
    
    # Validate all app configurations
    apps = data.get("apps", {})
    if not isinstance(apps, dict):
        logger.error("'apps' in config.json must be a JSON object.")
        sys.exit(1)
    
    for app_name, app_config in apps.items():
        validate_app_config(app_name, app_config)
    
    logger.debug(f"Loaded and validated configuration for {len(apps)} app(s).")
    return data


# ---------------------------------------------------------------------------
# PATH NORMALIZATION
# ---------------------------------------------------------------------------

def _normalize_path(value: Union[str, Path]) -> Path:
    """
    Convert config path values to Path, expanding ~ and env vars.
    
    Handles:
    - String paths with environment variables (e.g., $HOME, %APPDATA%)
    - Home directory expansion (~)
    - Relative and absolute paths
    - Existing Path objects
    
    We avoid strict resolve() so missing paths (to be deleted) don't error.
    On Windows, preserves drive letters and UNC paths.
    
    Args:
        value: String path or Path object to normalize
        
    Returns:
        Normalized Path object (may not exist)
        
    Raises:
        TypeError: If value is not str or Path
    """
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str):
        # Expand environment variables first (works on both Windows and Unix)
        expanded = os.path.expandvars(value)
        # Then expand user home directory
        candidate = Path(expanded).expanduser()
    else:
        raise TypeError(f"Path value must be str or Path, got {type(value)}")

    # On Windows, preserve absolute paths with drive letters
    # resolve(strict=False) will normalize but won't fail if path doesn't exist
    try:
        resolved = candidate.resolve(strict=False)
        # On Windows, ensure we preserve the absolute path structure
        # resolve() should handle this, but we verify
        if os.name == "nt" and candidate.is_absolute():
            # Ensure drive letter is preserved
            if not resolved.is_absolute() and candidate.drive:
                # Fallback: use original if resolve lost the drive
                return candidate
        return resolved
    except (RuntimeError, OSError):
        # resolve can fail on deeply nested symlinks or invalid paths
        # Return the expanded candidate as-is
        return candidate


def _normalize_path_list(values: Iterable[Union[str, Path]]) -> List[Path]:
    """
    Normalize a list of path values.
    
    Args:
        values: Iterable of strings or Path objects
        
    Returns:
        List of normalized Path objects
    """
    return [_normalize_path(v) for v in values]


# ---------------------------------------------------------------------------
# STATE STORAGE (SQLite3)
# ---------------------------------------------------------------------------

# Database schema version
DB_VERSION = 1

def get_db_path() -> Path:
    """
    Return path to SQLite database.
    
    Database is stored in:
    - Windows: %APPDATA%/.app_launch_tracker/state.db
    - Unix: ~/.app_launch_tracker/state.db
    
    Returns:
        Path to the database file
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home()
    
    state_dir = base / ".app_launch_tracker"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "state.db"


def get_db_version(db_path: Path) -> int:
    """
    Get the current database schema version.
    
    Args:
        db_path: Path to the database file
        
    Returns:
        Schema version number, or 0 if version table doesn't exist
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = cursor.fetchone()
            return row[0] if row else 0
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        return 0


def migrate_db(db_path: Path, from_version: int, to_version: int) -> None:
    """
    Migrate database schema from one version to another.
    
    Args:
        db_path: Path to the database file
        from_version: Current schema version
        to_version: Target schema version
    """
    if from_version >= to_version:
        return
    
    logger.info(f"Migrating database from version {from_version} to {to_version}")
    
    with sqlite3.connect(db_path) as conn:
        # Version 0 -> 1: Initial schema
        if from_version < 1:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS launches (
                    app_name TEXT PRIMARY KEY,
                    last_launch_iso TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    migrated_at TEXT NOT NULL
                )
            """)
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            conn.execute("INSERT INTO schema_version (version, migrated_at) VALUES (?, ?)", (1, now))
            conn.commit()
            logger.info("Database migrated to version 1")
        
        # Future migrations would go here:
        # if from_version < 2:
        #     # Add new columns, tables, etc.
        #     conn.execute("ALTER TABLE launches ADD COLUMN ...")
        #     ...


def init_db(db_path: Path) -> None:
    """
    Initialize the database schema if needed and run migrations.
    
    Args:
        db_path: Path to the database file
    """
    current_version = get_db_version(db_path)
    
    if current_version == 0:
        # First time setup
        migrate_db(db_path, 0, DB_VERSION)
    elif current_version < DB_VERSION:
        # Need to migrate
        migrate_db(db_path, current_version, DB_VERSION)
    elif current_version > DB_VERSION:
        logger.warning(f"Database version ({current_version}) is newer than code version ({DB_VERSION}).")
        logger.warning("Some features may not work correctly. Please update the application.")


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
        # 0o700: user rwx (not 0o777 for security)
        if path.exists():
            if path.is_file():
                path.chmod(0o600)  # User read/write only
            else:
                path.chmod(0o700)  # User rwx only 
    except (OSError, PermissionError):
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
    except (OSError, PermissionError, IOError) as e:
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
        except (OSError, PermissionError) as e:
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
    """
    Securely delete multiple paths.
    
    Args:
        paths: Iterable of paths (strings or Path objects) to delete
        passes: Number of overwrite passes for secure deletion (default: 3)
    """
    for p in paths:
        path_obj = _normalize_path(p)
        logger.info(f"Securely deleting: {path_obj}")
        secure_delete_path(path_obj, passes=passes)


# ---------------------------------------------------------------------------
# APP ACTIONS
# ---------------------------------------------------------------------------

def get_app_config(app_name: str) -> Dict[str, Any]:
    """
    Get configuration for a specific app.
    
    Args:
        app_name: Name of the app as defined in config.json
        
    Returns:
        Dictionary containing the app's configuration
        
    Raises:
        SystemExit: If app is not found in configuration
    """
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
    """
    Launch an application and record the launch time.
    
    The application is launched in a detached process and its launch time
    is recorded in the database for idle tracking.
    
    Args:
        app_name: Name of the app to launch (must exist in config.json)
        
    Raises:
        SystemExit: If app config is invalid or launch fails
    """
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
    """
    Check if an app has been idle too long and perform cleanup if needed.
    
    Compares the last launch time with max_days_idle from configuration.
    If the app has been idle longer than the threshold, securely deletes
    all paths specified in cleanup_paths.
    
    Args:
        app_name: Name of the app to check (must exist in config.json)
    """
    try:
        app = get_app_config(app_name)
        
        # Check if executable exists (app may have been uninstalled)
        raw_cmd = app.get("cmd")
        if raw_cmd:
            cmd = _normalize_cmd(raw_cmd)
            exe = cmd[0]
            resolved_exe = shutil.which(exe) or (exe if Path(exe).exists() else None)
            
            if not resolved_exe:
                logger.warning(f"Executable not found for '{app_name}': {exe}")
                logger.warning(f"App may have been uninstalled. Skipping task for '{app_name}'.")
                logger.warning("Consider removing this app from config.json or reinstalling the application.")
                return
        
        max_days = app.get("max_days_idle")
        cleanup_paths_raw = app.get("cleanup_paths", [])

        if cleanup_paths_raw and not isinstance(cleanup_paths_raw, (list, tuple)):
            logger.error(f"'cleanup_paths' for '{app_name}' must be a list.")
            sys.exit(1)

        cleanup_paths = _normalize_path_list(cleanup_paths_raw)

        if max_days is None:
            logger.warning(f"No 'max_days_idle' configured for '{app_name}'. Nothing to do.")
            return
        
        # Validate max_days_idle is a positive integer
        if not isinstance(max_days, int) or max_days <= 0:
            logger.error(f"'max_days_idle' for '{app_name}' must be a positive integer, got: {max_days}")
            sys.exit(1)

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
            try:
                secure_delete_paths(cleanup_paths, passes=3)
            except Exception as e:
                logger.error(f"Error during secure delete for '{app_name}': {e}")
                logger.exception("Full traceback:")
                # Continue execution - don't crash on cleanup errors
        else:
            logger.info(f"App '{app_name}' status: OK (Idle: {idle_days} vs Max: {max_days})")
    except Exception as e:
        logger.error(f"Unexpected error while processing task for '{app_name}': {e}")
        logger.exception("Full traceback:")
        return  # Don't crash, continue with other apps if task-all


def handle_task_all() -> None:
    """
    Run task for every configured app in config.json.
    
    Iterates through all apps defined in the configuration and runs
    handle_task() for each one to check idle status and perform cleanup.
    If one app fails, processing continues with the remaining apps.
    """
    try:
        config = load_config()
        apps = config.get("apps", {})
        if not apps:
            logger.info("No apps configured in config.json.")
            return
        
        logger.info(f"Processing {len(apps)} app(s)...")
        for app_name in apps:
            logger.info(f"Running task for '{app_name}'")
            try:
                handle_task(app_name)
            except Exception as e:
                logger.error(f"Failed to process '{app_name}': {e}")
                logger.exception("Full traceback:")
                # Continue with next app
                continue
        
        logger.info("Finished processing all apps.")
    except Exception as e:
        logger.error(f"Fatal error in handle_task_all: {e}")
        logger.exception("Full traceback:")
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def get_log_file_path() -> Path:
    """
    Get the path to the log file.
    
    Returns:
        Path to the log file in the user's home directory
    """
    if os.name == "nt":
        home_dir = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home_dir = Path.home()
    
    return home_dir / "app_launcher.log"


def main() -> None:
    """
    Main entry point for the application launcher CLI.
    
    Parses command-line arguments and dispatches to the appropriate handler.
    """
    # Log startup information
    log_file = get_log_file_path()
    logger.info(f"App Launcher started - Log file: {log_file}")
    
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

    try:
        if args.command == "launch":
            handle_launch(args.app_name)
        elif args.command == "task":
            handle_task(args.app_name)
        elif args.command == "task-all":
            handle_task_all()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        logger.exception("Full traceback:")
        sys.exit(1)
    finally:
        logger.info("App Launcher finished")

if __name__ == "__main__":
    main()
