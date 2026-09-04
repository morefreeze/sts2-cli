"""
Game logging — saves every state + action per run as JSONL for debugging.

Log files are stored in <project_root>/logs/ with auto-cleanup of old files.
"""

import json
import os
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")

# Default: keep logs for 7 days
LOG_RETENTION_DAYS = 7

# Keys the workbench catalog reads from the optional "run_meta" header line
# (see RunCatalog / _update_compact_replay_metadata). All are top-level, not
# nested under "data" -- that's what lets the catalog group runs into
# cohorts instead of degenerating to one cohort per file.
_RUN_META_KEYS = (
    "run_id",
    "batch_id",
    "experiment",
    "checkpoint",
    "character",
    "seed",
    "ascension",
    "game_version",
    "game_version_source",
    "evaluation_mode",
    "scenario",
)


def cleanup_old_logs(max_age_days=LOG_RETENTION_DAYS):
    """Remove log files older than max_age_days."""
    if not os.path.isdir(LOG_DIR):
        return
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for fname in os.listdir(LOG_DIR):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(LOG_DIR, fname)
        if os.path.getmtime(fpath) < cutoff:
            os.remove(fpath)
            removed += 1
    if removed:
        print(f"  [log] Cleaned up {removed} old log file(s)")


class GameLogger:
    """Logs every game step (state received + action sent) to a JSONL file."""

    def __init__(self, character: str, seed: str, enabled: bool = True,
                 run_context: dict | None = None):
        self.enabled = enabled
        self._step = 0
        self._file = None
        if not enabled:
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        cleanup_old_logs()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_seed = str(seed).replace("/", "_")
        filename = f"{ts}_{character}_{safe_seed}.jsonl"
        self._path = os.path.join(LOG_DIR, filename)
        self._file = open(self._path, "w")
        self._write_run_meta_header(character, seed, run_context)

    def _write_run_meta_header(self, character: str, seed: str,
                                run_context: dict | None):
        """Write one "run_meta" header line carrying batch/run identity.

        No-op unless run_context is a non-empty dict. Never advances
        self._step -- step numbering of subsequent state/action lines is
        unaffected. Logging must never break a run, so any failure here is
        swallowed.
        """
        if not run_context:
            return
        try:
            entry = {"type": "run_meta", "ts": time.time()}
            for key in _RUN_META_KEYS:
                value = run_context.get(key)
                if value is None and key in ("character", "seed"):
                    value = character if key == "character" else seed
                if value is not None:
                    entry[key] = value
            self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._file.flush()
        except Exception:
            pass

    def log_state(self, state: dict):
        """Log a state/decision point received from the simulator."""
        if not self.enabled or not self._file:
            return
        self._step += 1
        entry = {
            "step": self._step,
            "ts": datetime.now().isoformat(),
            "type": "state",
            "data": state,
        }
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()

    def log_action(self, action: dict):
        """Log an action/command sent to the simulator."""
        if not self.enabled or not self._file:
            return
        entry = {
            "step": self._step,
            "ts": datetime.now().isoformat(),
            "type": "action",
            "data": action,
        }
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()

    def log_map_snapshot(self, snapshot: dict):
        """Log one act's map snapshot into the replay.

        These previously went only to the deck-history file (combat_env's
        DECK_HISTORY_PATH branch), so a --game-log replay carried no map at all
        and the workbench — which keys its map view on event == "map_snapshot" —
        showed "缺失 · 完整地图分支" for every act of every run.
        """
        if not self.enabled or not self._file:
            return
        entry = {
            "step": self._step,
            "ts": datetime.now().isoformat(),
            "type": "map_snapshot",
            "data": snapshot,
        }
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None

    @property
    def path(self):
        return getattr(self, "_path", None)
