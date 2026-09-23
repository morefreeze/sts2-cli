"""The headless engine as a subprocess, with a per-reply watchdog.

agent/bug.md BUG-040: a plan_combat_turn call can hang forever -- one search
worker spins inside a single node expansion, where the solver's time budget is
never checked -- and a bare `proc.stdout.readline()` then blocks the harness
for as long as the engine spins (24 h, the first time). Here a reader thread
feeds a queue and every read has a deadline.

The engine runs in its own process group (start_new_session=True, the same
pattern agent/combat_env.py uses), because `dotnet run` is only a wrapper: the
process that actually spins is its child, and killing the wrapper alone would
orphan it at 100% CPU.
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time


class EngineHang(Exception):
    """The engine did not reply within the watchdog deadline.

    Deliberately NOT a RuntimeError, so an engine that exited (EOF,
    RuntimeError) can never be mistaken for one that hung.
    """


_EOF = object()


class EngineProcess:
    def __init__(self, argv, *, stderr=None):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(_EOF)

    def write(self, obj) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def read_json(self, timeout: float, on_skip=None) -> dict:
        """Next JSON reply, skipping non-JSON lines (build warnings etc.).

        Raises EngineHang if nothing JSON arrives within `timeout` seconds, and
        RuntimeError on EOF (the engine exited).
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineHang(f"no reply within {timeout:g}s")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise EngineHang(f"no reply within {timeout:g}s") from None
            if line is _EOF:
                raise RuntimeError("No response from simulator (EOF)")
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
            if line and on_skip is not None:
                on_skip(line)

    def kill(self) -> None:
        """Kill the whole process group -- the wrapper AND the engine under it."""
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        """Ask the engine to quit; if it doesn't, kill it. Safe after kill()."""
        if self.proc.poll() is None:
            try:
                self.write({"cmd": "quit"})
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()
        # Sweep the group either way: a child can outlive a wrapper that exited.
        # Safe from pid reuse -- a pgid cannot be recycled while any member of
        # the group is alive, and if none is, killpg just raises.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
