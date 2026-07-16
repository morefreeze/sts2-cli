#!/usr/bin/env zsh
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
exec "$repo_root/.venv/bin/python" -m agent.run_progress_autostart uninstall "$@"
