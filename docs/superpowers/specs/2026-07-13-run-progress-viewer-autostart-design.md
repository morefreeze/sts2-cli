# Run Progress Viewer Autostart Design

## Goal

Start the run progress analysis page automatically for the current macOS user after login.

## Scope

- Run `agent/run_progress_viewer.py` from this repository with `.venv/bin/python`.
- Bind to `127.0.0.1:8765`.
- Install a current-user LaunchAgent at `~/Library/LaunchAgents/com.sts2.run-progress-viewer.plist`.
- Keep the service running if it crashes.
- Write stdout and stderr logs under this repository's `logs/` directory.
- Do not open a browser automatically.
- Do not change training, evaluation, or gameplay policy code.

## Architecture

Add a small Python helper module that builds and installs the LaunchAgent plist. Shell scripts are thin wrappers around that module so the launchd configuration can be unit-tested without mutating the local machine.

## Components

- `agent/run_progress_autostart.py`
  - Builds the LaunchAgent dictionary.
  - Writes the plist with `plistlib`.
  - Installs, starts, stops, uninstalls, and reports launchd status.
- `scripts/install_run_progress_viewer_launch_agent.sh`
  - Calls the Python helper to install and start the agent.
- `scripts/uninstall_run_progress_viewer_launch_agent.sh`
  - Calls the Python helper to stop and remove the agent.
- `tests/agent/test_run_progress_autostart.py`
  - Verifies plist structure, paths, CLI dry-run output, and file writing.

## Error Handling

The installer creates `~/Library/LaunchAgents` and repo-local `logs/` if missing. It unloads any existing service with the same label before bootstrapping the new plist, so repeated installs replace stale config. It does not silently kill unrelated processes on port 8765.

## Testing

Unit tests cover plist generation and non-mutating CLI behavior. Manual verification after installation uses `launchctl print`, `lsof`, and an HTTP request to `http://127.0.0.1:8765/`.
