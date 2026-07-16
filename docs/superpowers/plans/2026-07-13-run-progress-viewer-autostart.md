# Run Progress Viewer Autostart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install the run progress analysis page as a current-user macOS LaunchAgent.

**Architecture:** Add a focused Python helper for plist generation and launchctl operations, then expose install/uninstall shell wrappers. Keep launchd behavior testable by separating plist construction from system mutation.

**Tech Stack:** Python `plistlib`, `argparse`, macOS `launchctl`, pytest, zsh shell wrappers.

---

### Task 1: LaunchAgent Model And CLI

**Files:**
- Create: `agent/run_progress_autostart.py`
- Create: `tests/agent/test_run_progress_autostart.py`

- [ ] **Step 1: Write failing tests**

Add tests that call `build_launch_agent_plist()` with a temporary repo root and assert the label, Python executable, viewer script path, host, port, working directory, log paths, `RunAtLoad`, and `KeepAlive`. Add a CLI dry-run test that verifies `print-plist` emits valid XML plist without touching launchctl.

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/python -m pytest tests/agent/test_run_progress_autostart.py -q`

Expected: failures because `agent.run_progress_autostart` does not exist.

- [ ] **Step 3: Implement helper module**

Create `agent/run_progress_autostart.py` with plist generation, plist writing, `install`, `uninstall`, `status`, and `print-plist` commands.

- [ ] **Step 4: Run tests and verify pass**

Run: `.venv/bin/python -m pytest tests/agent/test_run_progress_autostart.py -q`

Expected: all tests pass.

### Task 2: Shell Wrappers

**Files:**
- Create: `scripts/install_run_progress_viewer_launch_agent.sh`
- Create: `scripts/uninstall_run_progress_viewer_launch_agent.sh`

- [ ] **Step 1: Add wrappers**

Create executable shell scripts that resolve the repository root and call `.venv/bin/python -m agent.run_progress_autostart install` or `uninstall`.

- [ ] **Step 2: Verify wrapper syntax**

Run: `zsh -n scripts/install_run_progress_viewer_launch_agent.sh scripts/uninstall_run_progress_viewer_launch_agent.sh`

Expected: exit code 0.

### Task 3: Local Install And Verification

**Files:**
- Mutates local user file: `~/Library/LaunchAgents/com.sts2.run-progress-viewer.plist`

- [ ] **Step 1: Install LaunchAgent**

Run: `scripts/install_run_progress_viewer_launch_agent.sh`

Expected: plist is written, launchd service is bootstrapped, and kickstarted.

- [ ] **Step 2: Verify service**

Run: `launchctl print gui/$(id -u)/com.sts2.run-progress-viewer`

Expected: service exists under the current user GUI domain.

- [ ] **Step 3: Verify webpage**

Run: `curl -fsS http://127.0.0.1:8765/ >/tmp/sts2-viewer.html`

Expected: exit code 0 and non-empty HTML.
