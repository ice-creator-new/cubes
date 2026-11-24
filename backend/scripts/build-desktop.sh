#!/usr/bin/env bash
# Freeze the backend for the desktop app: one onedir tree the shell can start as a child.
# Run from anywhere; `npm run build:desktop` calls it right before electron-builder, which copies
# backend/build/desktop/dist/cubes-backend into Cubes.app/Contents/Resources/cubes-backend.
set -euo pipefail

backend_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$backend_dir/build/desktop"

# A stale tree would keep modules that were renamed, so it starts empty.
rm -rf "$work"
mkdir -p "$work"

cd "$backend_dir"
# resvg-py is a native extension loaded by the avatar rasterizer; uvicorn picks its protocol
# and loop implementations at runtime, so both are collected rather than guessed. websockets is
# only imported by uvicorn's WebSocket protocol (the sandbox's /computer/agent), so it is
# collected the same way. croniter imports dateutil lazily for cron rules, and tzdata supplies
# IANA zones on machines whose OS has no zoneinfo database, so all three are collected too.
# Pick the PyInstaller --add-data separator from the platform: ':' on POSIX, ';' on Windows.
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) DATA_SEP=";" ;; *) DATA_SEP=":" ;; esac
# Windows Python (which runs PyInstaller) cannot read Git Bash POSIX paths like /d/a/...,
# so translate them to native mixed form (D:/...) whenever we are on a Windows shell.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) native() { cygpath -m "$1"; } ;;
  *) native() { printf '%s' "$1"; } ;;
esac
backend_native="$(native "$backend_dir")"
work_native="$(native "$work")"
uv run --python 3.12 \
  --with-requirements requirements.txt \
  --with-requirements requirements-build.txt \
  pyinstaller \
  --noconfirm \
  --clean \
  --onedir \
  --name cubes-backend \
  --paths "$backend_native" \
  --collect-all resvg_py \
  --collect-submodules uvicorn \
  --collect-submodules websockets \
  --collect-submodules croniter \
  --collect-submodules dateutil \
  --collect-data tzdata \
  --add-data "${backend_native}/app/models_dev.json${DATA_SEP}app" \
  --distpath "$work_native/dist" \
  --workpath "$work_native/build" \
  --specpath "$work_native" \
  desktop_main.py

echo "backend bundle: $work/dist/cubes-backend"
