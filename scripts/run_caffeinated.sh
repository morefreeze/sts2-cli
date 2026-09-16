#!/bin/bash
# Wrap a long-running command with caffeinate so macOS doesn't idle-sleep mid-run
# (idle sleep after ~1min here freezes the process and can wipe /tmp).
# Usage: scripts/run_caffeinated.sh <command> [args...]
set -e
exec caffeinate -i "$@"
