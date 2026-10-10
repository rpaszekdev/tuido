#!/bin/sh
# Install TUiDO by putting the launcher on your PATH (~/.local/bin).
# Re-run this any time you move the repo.
set -e

REPO=$(cd -P "$(dirname "$0")" && pwd)
BIN="${HOME}/.local/bin"

mkdir -p "$BIN"
chmod +x "$REPO/bin/tuido"
ln -sfn "$REPO/bin/tuido" "$BIN/tuido"

# Remove a stale standalone copy left by older installs, if any.
if [ -f "${HOME}/.local/share/tuido/tuido.py" ]; then
    rm -f "${HOME}/.local/share/tuido/tuido.py"
fi

echo "installed: $BIN/tuido -> $REPO/bin/tuido"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "note: add $BIN to your PATH to run 'tuido' from anywhere." ;;
esac
