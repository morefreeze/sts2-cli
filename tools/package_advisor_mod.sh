#!/bin/bash
# tools/package_advisor_mod.sh -- build the Sts2Advisor mod (Release) and refresh the committed, ready-to-copy
# plugin directory mods/Sts2Advisor/ (Sts2Advisor.dll, Sts2Advisor.json, BUILD_INFO.txt). README.md in that
# directory is hand-written and left alone.
#
# Run it after any change under src/Sts2Advisor, src/Sts2Advisor.Core or src/Sts2Shared and commit the result,
# so a Windows (or any other) install via tools/install_advisor_mod.ps1 gets the same DLL. It only READS the
# game install (reference assemblies + release_info.json); it installs nothing and never touches a running game.
# The DLL is platform-neutral .NET; the mac game dir is just where the reference assemblies are found.
#
# By default it builds an export of git HEAD (`git archive`), NOT the working tree: other sessions' half-written
# edits must not end up in a committed DLL, and BUILD_INFO.txt's "source commit" is then exact. Commit the mod
# change first, then package. `--worktree` builds the working tree instead (BUILD_INFO then says so).
set -euo pipefail
FROM_WORKTREE=0
case "${1:-}" in
  "") ;;
  --worktree) FROM_WORKTREE=1 ;;
  *) echo "usage: $0 [--worktree]" >&2; exit 2 ;;
esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GAME_APP="${STS2_GAME_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app}"
DATA_DIR="${STS2_DATA_DIR:-$GAME_APP/Contents/Resources/data_sts2_macos_arm64}"
RELEASE_INFO="${STS2_RELEASE_INFO:-$GAME_APP/Contents/Resources/release_info.json}"
DOTNET="${DOTNET:-$HOME/.dotnet-arm64/dotnet}"
DEST="$ROOT/mods/Sts2Advisor"
SRC_COMMIT="$(git -C "$ROOT" rev-parse HEAD)"

if [ "$FROM_WORKTREE" = 1 ]; then
  BUILD_ROOT="$ROOT"
  SRC_NOTE=" + working tree (uncommitted changes included)"
else
  BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sts2advisor-package.XXXXXX")"
  trap 'rm -rf "$BUILD_ROOT"' EXIT
  git -C "$ROOT" archive HEAD src/Sts2Advisor src/Sts2Advisor.Core src/Sts2Shared | tar -x -C "$BUILD_ROOT"
  SRC_NOTE=""
  if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no -- src/Sts2Advisor src/Sts2Advisor.Core src/Sts2Shared)" ]; then
    echo "note: uncommitted changes in the mod sources are NOT in this build (it is git HEAD); use --worktree to include them" >&2
  fi
fi
OUT="$BUILD_ROOT/src/Sts2Advisor/bin/Release/net9.0"

"$DOTNET" build "$BUILD_ROOT/src/Sts2Advisor/Sts2Advisor.csproj" -c Release -p:Sts2DataDir="$DATA_DIR"

# Game version the DLL was built against (it must match on the machine that loads it).
GAME_VERSION="unknown"
GAME_COMMIT=""
if [ -f "$RELEASE_INFO" ]; then
  GAME_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version","unknown"))' "$RELEASE_INFO")"
  GAME_COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("commit",""))' "$RELEASE_INFO")"
else
  echo "warning: $RELEASE_INFO not found; BUILD_INFO.txt will say game version unknown" >&2
fi

mkdir -p "$DEST"
cp "$OUT/Sts2Advisor.dll" "$OUT/Sts2Advisor.json" "$DEST/"
MOD_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version","?"))' "$DEST/Sts2Advisor.json")"
cat > "$DEST/BUILD_INFO.txt" <<EOINFO
Sts2Advisor prebuilt plugin
mod version:   $MOD_VERSION
source commit: $SRC_COMMIT$SRC_NOTE
game version:  $GAME_VERSION${GAME_COMMIT:+ (game commit $GAME_COMMIT)}
built:         $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)
configuration: Release, net9.0, built against the Steam sts2.dll / GodotSharp.dll / 0Harmony.dll of the game version above
sha256:        $(shasum -a 256 "$DEST/Sts2Advisor.dll" | cut -d' ' -f1)  Sts2Advisor.dll

Install: tools/install_advisor_mod.ps1 (Windows) or tools/install_advisor_mod.sh (macOS). See README.md.
Refresh:  tools/package_advisor_mod.sh
EOINFO
echo "refreshed $DEST:"
ls -la "$DEST"
