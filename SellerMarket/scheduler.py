"""
Simple Scheduler for Trading Bot
Reads scheduler_config.json and executes jobs at scheduled times
"""

import glob
import gzip
import json
import os
import re
import time
import subprocess
import logging
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from threading import Thread, Event
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue #62: scheduled-run marker emission for the mgmt UI ingestor.
#
# We write a `scheduled_run_<id>.running.json` BEFORE the subprocess starts
# and replace it with a final `scheduled_run_<id>.json` once the subprocess
# returns (or times out). The mgmt UI's scheduled_run_ingestor polls the
# `run_results/` directory of every stack via SFTP, UPSERTs `runs` rows on
# the scheduled_run_id, and archives the captured tails into the local log
# blob store — exactly the same shape a manually-triggered run produces.
# ---------------------------------------------------------------------------

def _infer_mgmt_job_name(parsed_command) -> Optional[str]:
    """Map a scheduler.py job command to the mgmt UI's ``runs.job_name`` enum.

    The enum (see ``mgmt_ui/app/models/runs.py::run_job_name_enum``) is a
    closed set: ``cache_warmup`` and ``run_trading``. If the command is
    something else (test scripts, ad-hoc python, custom locust invocations)
    we return ``None`` and silently skip marker emission so unrelated jobs
    don't surface as broken rows in the mgmt UI.
    """
    if not parsed_command:
        return None
    executable = parsed_command[0]
    if executable == "python":
        for arg in parsed_command[1:]:
            if arg.endswith("cache_warmup.py"):
                return "cache_warmup"
            # The Mofid firer (python run_mofid.py) IS a trading run for the
            # Mofid family (it can't ride locust — see run_mofid.py). Surface it
            # in the mgmt Runs list under the existing run_trading enum so no
            # mgmt-side runs.job_name migration is needed.
            if arg.endswith("run_mofid.py"):
                return "run_trading"
    if executable == "locust":
        return "run_trading"
    return None


def _emit_scheduled_run_marker(path: str, payload: Dict[str, Any]) -> bool:
    """Atomic-ish write of a scheduled-run marker JSON.

    Writes to a temp file in the same directory and ``os.replace``s it
    over the target — that's atomic on the same filesystem and avoids
    the ingestor ever reading a half-written file. Marker emission is
    best-effort: if we can't create the directory or write the file we
    log and continue, never propagate. The scheduled job MUST run even
    if the mgmt UI plumbing is broken.

    Returns ``True`` on success, ``False`` on any failure. Callers that
    delete the running marker after writing the final one MUST gate the
    delete on this return — otherwise a failed final-write leaves the
    mgmt UI's row stuck at status='running' forever.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — never let marker I/O block the job
        logger.exception("failed to emit scheduled-run marker at %s", path)
        return False


# Separator between the stdout and stderr sections of a combined run log.
# MUST stay byte-identical to what the mgmt UI writes for manual runs
# (services/runs.py::finalize_run / scheduled_run_ingestor._archive_log_if_final)
# so every archived run log has one uniform shape.
_RUN_LOG_STDERR_SEPARATOR = b"\n--- stderr ---\n"
# Refuse to gzip absurdly large captures (runaway subprocess output).
_RUN_LOG_MAX_BYTES = 128 * 1024 * 1024
# Orphan .log.gz cleanup horizon: files the mgmt ingestor never consumed
# (old mgmt image, mgmt down, fetch fallback) must not pile up forever.
_RUN_LOG_GZ_MAX_AGE_DAYS = 7


def _write_scheduled_run_log_gz(path: str, stdout: str, stderr: str) -> bool:
    """Write the run's FULL combined output, gzip-compressed, atomically.

    The mgmt UI's scheduled_run_ingestor fetches this over SFTP and archives
    it verbatim so the operator can download the complete log from the Runs
    page (the marker itself only carries 4 KB tails as a fallback). Returns
    True on success; best-effort, never raises.
    """
    try:
        out = (stdout or "").encode("utf-8", errors="replace")
        err = (stderr or "").encode("utf-8", errors="replace")
        blob = out + (_RUN_LOG_STDERR_SEPARATOR + err if err else b"")
        if len(blob) > _RUN_LOG_MAX_BYTES:
            logger.warning(
                "scheduled-run log too large to archive (%d bytes > %d) — skipping",
                len(blob), _RUN_LOG_MAX_BYTES,
            )
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wb") as gz:
            gz.write(blob)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — log archiving must never block the job
        logger.exception("failed to write scheduled-run log gz at %s", path)
        return False


def _prune_old_run_log_gz(run_results_dir: str, max_age_days: int = _RUN_LOG_GZ_MAX_AGE_DAYS) -> None:
    """Delete orphaned scheduled_run_*.log.gz older than ``max_age_days``."""
    try:
        cutoff = time.time() - max_age_days * 86400
        for path in glob.glob(os.path.join(run_results_dir, "scheduled_run_*.log.gz")):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        logger.debug(
            "failed to prune scheduled_run_*.log.gz under %s",
            run_results_dir, exc_info=True,
        )


def load_locust_config() -> Dict[str, Any]:
    """
    Load Locust configuration from a locust_config.json file located next to this module.
    
    Attempts to read the file and return the value of the top-level "locust" key as a dict. If the file is missing or contains invalid JSON, logs a warning and returns default Locust parameters.
    
    Returns:
        dict: A mapping of Locust parameters. Expected keys include:
            - "users" (int): number of users, default 10
            - "spawn_rate" (int): spawn rate, default 10
            - "run_time" (str): run time string, default "30s"
            - "host" (str): target host URL, default "https://abc.com"
            - "processes" (int): number of worker processes for distributed load, optional
              Use -1 for auto-detect CPU cores. Note: requires Linux/macOS (uses fork())
    """
    locust_config_file = os.path.join(os.path.dirname(__file__), 'locust_config.json')
    try:
        with open(locust_config_file, 'r') as f:
            config = json.load(f)
        return config.get('locust', {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load locust_config.json: {e}. Using defaults.")
        return {
            'users': 10,
            'spawn_rate': 10,
            'run_time': '30s',
            'host': 'https://abc.com'
        }

def build_locust_command_from_config(base_command: str) -> List[str]:
    """
    Builds a complete Locust CLI command as a list of arguments by appending configured parameters from locust_config.json to a base locust invocation.
    
    Parameters:
        base_command (str): The base command string (e.g. "locust -f locustfile.py --headless"); if it does not start with "locust", it is returned unchanged.
    
    Returns:
        full_command_args (List[str]): The combined command arguments including any of `--users`, `--spawn-rate`, `--run-time`, `--host`, and `--processes` present in the Locust config.
    """
    locust_config = load_locust_config()
    
    # Parse base command to check if it's a locust command
    parts = shlex.split(base_command)
    if not parts or parts[0] != 'locust':
        return shlex.split(base_command) if isinstance(base_command, str) else base_command
    
    # Start with base command parts
    command_args = parts.copy()
    
    # Build additional parameters from config as separate list elements
    if 'users' in locust_config:
        command_args.extend(['--users', str(locust_config['users'])])
    
    if 'spawn_rate' in locust_config:
        command_args.extend(['--spawn-rate', str(locust_config['spawn_rate'])])
    
    if 'run_time' in locust_config:
        command_args.extend(['--run-time', str(locust_config['run_time'])])
    
    if 'host' in locust_config:
        command_args.extend(['--host', str(locust_config['host'])])
    
    # Add --processes for distributed load generation (Linux/macOS only, uses fork())
    if 'processes' in locust_config:
        command_args.extend(['--processes', str(locust_config['processes'])])
    
    logger.info(f"Built Locust command from config: {shlex.join(command_args)}")
    return command_args


DEFAULT_JOB_TIMEOUT_SECONDS = 600
JOB_TIMEOUT_GRACE_SECONDS = 180


def _parse_locust_duration(value) -> Optional[int]:
    """Parse a locust --run-time value to seconds.

    Accepts locust's duration syntax ("599s", "10m", "1h30m") and a bare
    integer ("90"). Returns None for anything unparseable or zero.
    """
    s = str(value).strip().lower()
    if not s:
        return None
    if s.isdigit():
        return int(s) or None
    matched = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s)
    if not matched or not any(matched.groups()):
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in matched.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return total or None


def _compute_job_timeout(parsed_command: List[str],
                         default: int = DEFAULT_JOB_TIMEOUT_SECONDS,
                         grace: int = JOB_TIMEOUT_GRACE_SECONDS) -> int:
    """Wall-clock cap for a scheduled job's subprocess.

    At least `default`; for locust commands carrying --run-time, the cap is
    max(default, run_time + grace) — a hard default of 600s would kill a
    configured 599s trading run BEFORE locust's on_test_stop runs, losing the
    fire-log flush and order_results (the mgmt UI pushes operator-tunable
    run_time via locust_config.json, so the cap must follow it).

    For the Mofid firer (python run_mofid.py) the cap must reach the FIRE WINDOW:
    run_mofid starts at mofid_run_time, creates the drafts, then waits IN-PROCESS
    until the (server-time-synced) window to batch-send. A run_time set well
    before the window (e.g. 08:30 for a 08:45 window) exceeds the 600s default and
    the subprocess is killed BEFORE it can fire — so cap at window_end + grace.
    """
    if any(str(a).endswith("run_mofid.py") for a in parsed_command):
        try:
            import mofid_firer
            secs = (mofid_firer.window_end_local_ms()
                    - int(datetime.now().timestamp() * 1000)) / 1000.0
            return max(default, int(secs) + grace)
        except Exception:
            logger.debug("run_mofid window-timeout compute failed", exc_info=True)
            return max(default, 1800)  # generous fallback covers an early run_time
    try:
        for i, arg in enumerate(parsed_command):
            run_time = None
            if arg == "--run-time" and i + 1 < len(parsed_command):
                run_time = _parse_locust_duration(parsed_command[i + 1])
            elif arg.startswith("--run-time="):
                run_time = _parse_locust_duration(arg.split("=", 1)[1])
            if run_time:
                return max(default, run_time + grace)
    except Exception:
        logger.debug(
            "_compute_job_timeout: failed to parse --run-time from %s",
            parsed_command, exc_info=True,
        )
    return default


class JobScheduler:
    """Simple job scheduler that runs in a background thread"""
    
    def __init__(self, config_file: str):
        self.config_file = config_file
        self.stop_event = Event()
        self.thread = None
        self.executed_today = {}  # Track which jobs ran today
        
    def load_config(self) -> Dict[str, Any]:
        """Load scheduler configuration"""
        try:
            if not os.path.exists(self.config_file):
                logger.warning(f"Scheduler config not found: {self.config_file}")
                return {"enabled": False, "jobs": []}
            
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            
            return config
        except Exception as e:
            logger.error(f"Error loading scheduler config: {e}")
            return {"enabled": False, "jobs": []}
    
    def should_run_job(self, job: Dict[str, Any]) -> bool:
        """Check if a job should run now within the allowed time window"""
        try:
            if not job.get('enabled', True):
                return False
            
            # Parse job time
            job_time_str = job['time']  # Format: "HH:MM:SS"
            job_time = datetime.strptime(job_time_str, '%H:%M:%S').time()
            
            # Get current time
            now = datetime.now()
            current_time = now.time()
            today = now.date()
            
            # Check if already executed today for this scheduled time.
            # Key includes job_time_str so that updating the schedule on the same day
            # produces a new key and allows the job to run again at the new time.
            job_key = f"{job['name']}_{today.isoformat()}_{job_time_str}"
            if job_key in self.executed_today:
                return False
            
            # Build full datetime objects for proper comparison across midnight
            job_dt = datetime.combine(today, job_time)
            current_dt = datetime.combine(today, current_time)
            
            # If job datetime is in the future, it might be scheduled for yesterday
            # (e.g., job at 23:59:00, current time is 00:01:00)
            if job_dt > current_dt:
                job_dt = job_dt - timedelta(days=1)
            
            # Calculate time difference
            delta = current_dt - job_dt
            
            # Only run if we're within 120 seconds AFTER the scheduled time
            # This prevents immediate execution when bot starts after scheduled time
            # Jobs that were missed (more than 120 seconds ago) will wait until next day
            if 0 <= delta.total_seconds() <= 120:
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking job schedule: {e}")
            return False
    
    def execute_job(self, job: Dict[str, Any]):
        """
        Run a scheduled job command if it meets validation and record its execution for today.
        
        This method accepts a job mapping with keys "name" and "command", optionally rewrites Locust commands using locust configuration, validates that the command is non-empty and the executable is in a small whitelist, records the job as executed for the current date to prevent duplicate runs, and executes the command as a subprocess with a 10-minute timeout. Outcomes (success, failure, timeout, or validation errors) are logged. Any exceptions during execution are caught and logged; the method does not raise.
        
        Parameters:
            job (Dict[str, Any]): Job definition containing:
                - "name" (str): Human-readable job name used in logs and as part of the once-per-day key.
                - "command" (str | Sequence[str]): Command to execute; if a string beginning with "locust", the command may be expanded from locust_config.json.
        
        Side effects:
            - Updates self.executed_today to mark the job as run for today's date.
            - Spawns a subprocess to run the validated command.
            - Writes informational, debug, and error logs describing validation and execution results.
        """
        try:
            job_name = job['name']
            command = job['command']
            
            # If this is a locust command, build full command from locust_config.json
            if isinstance(command, str) and command.strip().startswith('locust'):
                command = build_locust_command_from_config(command)
            
            # Convert command to list format for logging and validation
            if isinstance(command, str):
                command_for_logging = command
                try:
                    parsed_command = shlex.split(command)
                except ValueError as e:
                    logger.error(f"❌ Invalid command syntax for job '{job_name}': {e}")
                    return
            else:
                # command is already a list from build_locust_command_from_config
                parsed_command = command
                command_for_logging = shlex.join(command)
            
            logger.info(f"⏰ Executing scheduled job: {job_name}")
            logger.info(f"   Command: {command_for_logging}")
            
            # Validate and parse command
            allowed_binaries = {'python', 'locust'}  # Whitelist of allowed executables
            
            if not parsed_command:
                logger.error(f"❌ Empty command for job '{job_name}'")
                return
            
            # Validate executable is in whitelist
            executable = parsed_command[0]
            if executable not in allowed_binaries:
                logger.error(f"❌ Command '{executable}' not in allowed binaries for job '{job_name}'")
                return
            
            # Mark job as executed BEFORE running to prevent multiple attempts on the same day.
            # This is intentional: scheduled jobs (cache warmup, trading) should only run once per day
            # regardless of success/failure. Failures are logged but don't trigger retries.
            # Key includes the scheduled time so that updating job['time'] mid-day produces a new
            # key and allows the job to fire again at the new schedule.
            now = datetime.now()
            job_time_str = job.get('time', '')
            job_key = f"{job_name}_{now.date().isoformat()}_{job_time_str}"
            self.executed_today[job_key] = now

            # Issue #62: emit a marker file the mgmt UI's scheduled_run_ingestor
            # picks up so this scheduled fire shows in the Runs list. We pick a
            # UUID4 here that becomes the mgmt-side `runs.id` (UPSERTed by the
            # ingestor) so the running → terminal transition is idempotent.
            # The "mgmt_job_name" maps the local job spec to the enum the mgmt
            # UI's runs table expects (cache_warmup / run_trading). Jobs that
            # don't match either get None and are silently NOT tracked — the
            # ingestor only knows those two kinds today.
            scheduled_run_id = str(uuid.uuid4())
            mgmt_job_name = _infer_mgmt_job_name(parsed_command)
            started_at_iso = datetime.now(timezone.utc).isoformat()
            run_results_dir = os.path.join(os.path.dirname(__file__), "run_results")
            running_marker = os.path.join(run_results_dir, f"scheduled_run_{scheduled_run_id}.running.json")
            final_marker = os.path.join(run_results_dir, f"scheduled_run_{scheduled_run_id}.json")
            if mgmt_job_name is not None:
                _emit_scheduled_run_marker(
                    running_marker,
                    {
                        "schema_version": 1,
                        "scheduled_run_id": scheduled_run_id,
                        "job_name": mgmt_job_name,
                        "trigger": "scheduled",
                        "started_at": started_at_iso,
                        "command": command_for_logging,
                        "status": "running",
                    },
                )

            # Execute command with current environment variables (shell=False for security)
            job_timeout_s = _compute_job_timeout(parsed_command)
            start_ts = time.monotonic()
            result = subprocess.run(
                parsed_command,
                shell=False,
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=job_timeout_s,  # default 600s; follows locust --run-time + grace
                env=os.environ.copy()  # Pass environment variables to subprocess
            )
            elapsed_s = time.monotonic() - start_ts
            finished_at_iso = datetime.now(timezone.utc).isoformat()

            if result.returncode == 0:
                logger.info(f"✅ Job '{job_name}' completed successfully in {elapsed_s:.1f}s")
                # Log FULL stdout at DEBUG so an operator running with -v can see
                # what the warmup / locust subprocess actually did. The previous
                # 500-char tail hid early-step failures that "succeeded" overall.
                logger.debug(f"Full stdout ({len(result.stdout)} chars):\n{result.stdout}")
                if result.stderr:
                    logger.debug(f"stderr ({len(result.stderr)} chars):\n{result.stderr}")
            else:
                logger.error(f"❌ Job '{job_name}' failed with return code {result.returncode} "
                            f"after {elapsed_s:.1f}s")
                logger.error(f"stderr tail: {result.stderr[-500:]}")
                logger.debug(f"Full stdout ({len(result.stdout)} chars):\n{result.stdout}")
                logger.debug(f"Full stderr ({len(result.stderr)} chars):\n{result.stderr}")

            # Final marker for the mgmt UI ingestor. The FULL combined output
            # is written alongside as a gzipped log file the ingestor fetches
            # over SFTP (operator decision: keep complete run logs — they're
            # needed for bug investigation). The 4 KB stdout/stderr tails stay
            # in the marker as the fallback for fetch failures / old mgmt.
            if mgmt_job_name is not None:
                _prune_old_run_log_gz(run_results_dir)
                log_gz_name = f"scheduled_run_{scheduled_run_id}.log.gz"
                log_gz_written = _write_scheduled_run_log_gz(
                    os.path.join(run_results_dir, log_gz_name),
                    result.stdout or "",
                    result.stderr or "",
                )
                final_payload = {
                    "schema_version": 1,
                    "scheduled_run_id": scheduled_run_id,
                    "job_name": mgmt_job_name,
                    "trigger": "scheduled",
                    "started_at": started_at_iso,
                    "finished_at": finished_at_iso,
                    "elapsed_seconds": round(elapsed_s, 3),
                    "exit_code": int(result.returncode),
                    "status": "success" if result.returncode == 0 else "failed",
                    "stdout_tail": (result.stdout or "")[-4096:],
                    "stderr_tail": (result.stderr or "")[-4096:],
                    "command": command_for_logging,
                }
                if log_gz_written:
                    final_payload["log_file"] = log_gz_name
                final_written = _emit_scheduled_run_marker(final_marker, final_payload)
                # Only delete the running marker if the final marker
                # actually persisted. Otherwise a transient disk-full /
                # permission glitch would leave the mgmt UI's row stuck
                # at status='running' AND no terminal marker for the
                # ingestor to ever pick up.
                if final_written:
                    try:
                        os.remove(running_marker)
                    except OSError:
                        pass
                else:
                    logger.warning(
                        "final marker write failed for scheduled_run_id=%s — "
                        "leaving running marker in place so a future tick can retry",
                        scheduled_run_id,
                    )

        except subprocess.TimeoutExpired:
            timeout_desc = (f"{job_timeout_s} seconds" if 'job_timeout_s' in locals()
                            else "the configured timeout")
            logger.error(f"⏱️ Job '{job_name}' timed out after {timeout_desc}")
            # If we already wrote a running marker, replace it with a
            # timeout-final marker so the mgmt UI doesn't show this run as
            # stuck-running forever.
            try:
                if 'mgmt_job_name' in locals() and mgmt_job_name is not None:
                    final_written = _emit_scheduled_run_marker(
                        final_marker,
                        {
                            "schema_version": 1,
                            "scheduled_run_id": scheduled_run_id,
                            "job_name": mgmt_job_name,
                            "trigger": "scheduled",
                            "started_at": started_at_iso,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "exit_code": -1,
                            "status": "failed",
                            "stdout_tail": "",
                            "stderr_tail": f"subprocess timed out after {timeout_desc}",
                            "command": command_for_logging,
                        },
                    )
                    # Same gate as the happy path — never strand the running
                    # marker if the final write didn't land.
                    if final_written:
                        try:
                            os.remove(running_marker)
                        except OSError:
                            pass
            except Exception:
                logger.exception("failed to emit timeout marker for scheduled_run")
        except Exception as e:
            logger.error(f"❌ Error executing job '{job_name}': {e}")
    
    def run(self):
        """Main scheduler loop"""
        logger.info("📅 Scheduler started")
        
        while not self.stop_event.is_set():
            try:
                # Load current configuration
                config = self.load_config()
                
                if not config.get('enabled', False):
                    # Sleep for 60 seconds if disabled
                    time.sleep(60)
                    continue
                
                # Check each job
                jobs = config.get('jobs', [])
                for job in jobs:
                    if self.should_run_job(job):
                        self.execute_job(job)
                
                # Clean up old execution records (older than today).
                # Filter by the stored execution datetime since the key no longer ends with the date.
                today_date = datetime.now().date()
                self.executed_today = {
                    k: v for k, v in self.executed_today.items()
                    if v.date() == today_date
                }
                
                # Sleep for 1 second before next check
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(60)
        
        logger.info("📅 Scheduler stopped")
    
    def start(self):
        """Start scheduler in background thread"""
        if self.thread and self.thread.is_alive():
            logger.warning("Scheduler already running")
            return
        
        self.stop_event.clear()
        self.thread = Thread(target=self.run, daemon=True, name="JobScheduler")
        self.thread.start()
        logger.info("📅 Scheduler thread started")
    
    def stop(self):
        """Stop scheduler"""
        if self.thread and self.thread.is_alive():
            logger.info("Stopping scheduler...")
            self.stop_event.set()
            self.thread.join(timeout=5)
            logger.info("📅 Scheduler stopped")
    
    def reload_config(self):
        """
        Force the scheduler to pick up configuration changes.
        
        This method trims the executed_today cache to retain only today's execution records,
        removing entries from previous days. This allows the scheduler loop—which already
        rereads the config each iteration (every 10 seconds)—to immediately reflect config
        changes while keeping execution tracking accurate for the current day.
        
        The run loop already rereads the config file on each iteration (every 10 seconds),
        so this method just ensures that execution tracking is up-to-date.
        """
        today_date = datetime.now().date()
        # Keep only jobs that were already executed today (filter by stored timestamp).
        self.executed_today = {
            k: v for k, v in self.executed_today.items()
            if v.date() == today_date
        }
        logger.info("📅 Scheduler configuration reloaded, execution cache refreshed")