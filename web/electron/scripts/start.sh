#!/bin/sh
# `npm run desktop`: Electron on the source tree, as its own responsible app (see own.py), so
# screen recording and accessibility are granted to Electron, not to the terminal.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
electron="$here/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
if [ ! -x "$electron" ]; then
  echo "没有找到 Electron：先在 web/ 里执行 npm --prefix electron install。" >&2
  exit 1
fi
python="$here/../../backend/.venv/bin/python"
[ -x "$python" ] || python=/usr/bin/python3
if [ "$(uname)" = Darwin ] && [ -x "$python" ]; then
  exec "$python" "$here/scripts/own.py" "$electron" "$here" "$@"
fi
exec "$electron" "$here" "$@"
