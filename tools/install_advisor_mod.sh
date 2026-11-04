#!/bin/bash
# tools/install_advisor_mod.sh -- build the Sts2Advisor mod and install it into the local Steam copy.
# Re-run after a Steam update or "verify integrity of game files" (either can remove mods/).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GAME_APP="${STS2_GAME_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app}"
DATA_DIR="$GAME_APP/Contents/Resources/data_sts2_macos_arm64"
MODS_DIR="$GAME_APP/Contents/MacOS/mods/Sts2Advisor"
USER_DIR="${STS2_USER_DIR:-$HOME/Library/Application Support/SlayTheSpire2}/Sts2Advisor"
DOTNET="${DOTNET:-$HOME/.dotnet-arm64/dotnet}"

"$DOTNET" build "$ROOT/src/Sts2Advisor/Sts2Advisor.csproj" -c Release -p:Sts2DataDir="$DATA_DIR"
OUT="$ROOT/src/Sts2Advisor/bin/Release/net9.0"
# Overwriting the DLL the game has loaded corrupts it, so refuse while the game runs (override: STS2_INSTALL_WHILE_RUNNING=1).
if [ "${STS2_INSTALL_WHILE_RUNNING:-}" != "1" ] && pgrep -f 'SlayTheSpire2.app/Contents/MacOS/[S]lay the Spire 2' >/dev/null; then
  echo "Slay the Spire 2 is running: overwriting the loaded Sts2Advisor.dll corrupts it (BadImageFormatException 'Bad IL range' on every not-yet-JIT-compiled method). Quit the game first (campaign.json \"command\": \"quit\"), then re-run." >&2
  exit 1
fi
mkdir -p "$MODS_DIR"
cp "$OUT/Sts2Advisor.dll" "$OUT/Sts2Advisor.json" "$MODS_DIR/"

mkdir -p "$USER_DIR"
if [ ! -f "$USER_DIR/config.json" ]; then
  cat > "$USER_DIR/config.json" <<EOCFG
{
  "repo_path": "$ROOT",
  "python": "$ROOT/.venv/bin/python",
  "port": 8767,
  "autostart": true,
  "hotkey": "F8",
  "autopilot_hotkey": "F9",
  "step_hotkey": "F10",
  "step_pause_seconds": 2.5,
  "panel_language": "auto",
  "map_policy": "lookahead"
}
EOCFG
  echo "wrote $USER_DIR/config.json"
fi
echo "installed to $MODS_DIR"
ls -la "$MODS_DIR"
