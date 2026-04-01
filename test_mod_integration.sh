#!/bin/bash
# Integration test for Sts2CliMod
# This script verifies that all components build and are in correct locations

set -e

echo "=== Sts2CliMod Integration Test ==="
echo

echo "1. Building Sts2Headless project..."
dotnet build src/Sts2Headless/Sts2Headless.csproj > /dev/null 2>&1
echo "   ✓ Sts2Headless built successfully"

echo
echo "2. Building Sts2CliMod project..."
dotnet build src/Sts2CliMod/Sts2CliMod.csproj > /dev/null 2>&1
echo "   ✓ Sts2CliMod built successfully"

echo
echo "3. Checking mod files..."
MOD_DIR="src/Sts2CliMod/Sts2CliMod"
if [ -f "$MOD_DIR/Sts2CliMod.dll" ]; then
    echo "   ✓ Sts2CliMod.dll exists"
    SIZE=$(stat -f%z "$MOD_DIR/Sts2CliMod.dll" 2>/dev/null || stat -c%s "$MOD_DIR/Sts2CliMod.dll" 2>/dev/null)
    echo "     Size: $SIZE bytes"
else
    echo "   ✗ Sts2CliMod.dll not found"
    exit 1
fi

if [ -f "$MOD_DIR/Sts2CliMod.json" ]; then
    echo "   ✓ Sts2CliMod.json (manifest) exists"
else
    echo "   ✗ Sts2CliMod.json not found"
    exit 1
fi

echo
echo "4. Checking Python client..."
if [ -f "python/sts2_mod_client.py" ]; then
    echo "   ✓ sts2_mod_client.py exists"
    if python3 -m py_compile python/sts2_mod_client.py 2>/dev/null; then
        echo "   ✓ Python client syntax is valid"
    else
        echo "   ✗ Python client has syntax errors"
        exit 1
    fi
else
    echo "   ✗ sts2_mod_client.py not found"
    exit 1
fi

echo
echo "5. Checking core library..."
if [ -f "src/Sts2HeadlessCore/bin/Debug/net9.0/Sts2HeadlessCore.dll" ]; then
    echo "   ✓ Sts2HeadlessCore.dll exists"
else
    echo "   ✗ Sts2HeadlessCore.dll not found"
    exit 1
fi

echo
echo "6. Verifying key source files..."
FILES=(
    "src/Sts2CliMod/Server/EmbeddedServer.cs"
    "src/Sts2CliMod/Hooks/ModHooks.cs"
    "src/Sts2CliMod/MainFile.cs"
    "src/Sts2HeadlessCore/Core/RunSimulator.cs"
    "src/Sts2HeadlessCore/Localization/LocLookup.cs"
)

for file in "${FILES[@]}"; do
    if [ -f "$file" ]; then
        echo "   ✓ $file"
    else
        echo "   ✗ $file not found"
        exit 1
    fi
done

echo
echo "7. Checking project structure..."
# Check that EmbeddedServer has the required methods
if grep -q "ExecuteCommand" src/Sts2CliMod/Server/EmbeddedServer.cs; then
    echo "   ✓ EmbeddedServer.ExecuteCommand exists"
else
    echo "   ✗ EmbeddedServer.ExecuteCommand not found"
    exit 1
fi

if grep -q "SetCurrentState" src/Sts2CliMod/Server/EmbeddedServer.cs; then
    echo "   ✓ EmbeddedServer.SetCurrentState exists"
else
    echo "   ✗ EmbeddedServer.SetCurrentState not found"
    exit 1
fi

# Check that RunSimulator is connected to EmbeddedServer
if grep -q "SetSimulator" src/Sts2CliMod/MainFile.cs; then
    echo "   ✓ MainFile connects RunSimulator to EmbeddedServer"
else
    echo "   ✗ MainFile doesn't connect RunSimulator"
    exit 1
fi

# Check that Python client has helper methods
if grep -q "async def start_run" python/sts2_mod_client.py; then
    echo "   ✓ Python client has start_run helper"
else
    echo "   ✗ Python client missing start_run helper"
    exit 1
fi

echo
echo "=== All Integration Tests Passed ==="
echo
echo "Mod Installation:"
echo "  Location: $MOD_DIR/"
echo "  Files: Sts2CliMod.dll, Sts2CliMod.json"
echo
echo "To use the mod:"
echo "  1. Copy $MOD_DIR/ to your game's mods/ directory"
echo "  2. Launch the game"
echo "  3. Test with: python3 python/sts2_mod_client.py --health"
echo
