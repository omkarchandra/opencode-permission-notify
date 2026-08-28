#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV="$ROOT/.venv"

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/pip" install --editable "$ROOT"

mkdir -p "$HOME/.local/bin"
ln -sfn "$VENV/bin/ocdeck" "$HOME/.local/bin/ocdeck"

printf 'Installed OC Deck. Run: ocdeck\n'
