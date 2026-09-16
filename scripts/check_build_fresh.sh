#!/bin/bash
# Warn if C# source files are newer than the last build output — running a
# stale build against fresh seeds/checkpoints can look like new "technical
# failures" that are actually already-fixed bugs from before the last edit.
set -e
cd "$(dirname "$0")/.."

BUILD_OUTPUT=$(find src/Sts2Headless/bin -name "Sts2Headless.dll" 2>/dev/null | head -1)

if [ -z "$BUILD_OUTPUT" ]; then
    echo "WARNING: no build output found under src/Sts2Headless/bin — run: dotnet build src/Sts2Headless/Sts2Headless.csproj"
    exit 1
fi

NEWEST_SRC=$(find src/Sts2Headless src/GodotStubs -name "*.cs" -newer "$BUILD_OUTPUT" 2>/dev/null | head -1)
if [ -n "$NEWEST_SRC" ]; then
    echo "WARNING: $NEWEST_SRC is newer than the build output ($BUILD_OUTPUT)."
    echo "Run: dotnet build src/Sts2Headless/Sts2Headless.csproj"
    exit 1
fi

echo "Build is up to date."
exit 0
