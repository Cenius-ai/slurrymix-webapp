#!/usr/bin/env bash
# Slurrymix end-to-end demo.
#
# Runs the real CLI against a throwaway state file in a temporary directory, so
# the repository and your working directory are left untouched. Non-interactive:
# the wizard step is fed with blank lines, which accepts every default.
#
#   bash demo.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: $PY not found - install Python 3.10 or newer (see INSTALL.md)" >&2
  exit 1
fi

WORK="$(mktemp -d)"
STATE="$WORK/slurrymix.json"
REPORTS="$WORK/reports"
trap 'rm -rf "$WORK"' EXIT

YEAR="$(date +%Y)"
MAR="$(printf '%s-03-14' "$YEAR")"
APR="$(printf '%s-04-02' "$YEAR")"
MAY="$(printf '%s-05-06' "$YEAR")"

sm() { "$PY" slurrymix.py --state "$STATE" "$@"; }
section() { printf '\n== %s\n' "$1"; }

printf '\n== Slurrymix demo (%s)\n' "$("$PY" slurrymix.py --version)"
"$PY" slurrymix.py --help | sed -n '1,12p'

section "first run: nothing stored yet"
sm status

section "register the fields"
sm field add --name "North 12" --area 4.5 --crop wheat --target-n 170
sm field add --name "South 3" --area 2.5 --crop grass --target-n 120
sm field add --name "Riverside 7" --area 6.0 --crop maize --target-n 160

section "record the slurry analyses"
sm batch add --label "Pit A March" --source cattle --n 4.2 --dm 8.5 --sampled "$MAR"
sm batch add --label "Lagoon B April" --source pig --n 3.6 --dm 6.0 --sampled "$APR"

section "define the stores and their levels"
sm storage add --name "Lagoon 1" --capacity 1200
sm storage add --name "Slurry pit" --capacity 450
sm storage set --name "Lagoon 1" --level 950
sm storage set --name "Slurry pit" --level 380

section "the nitrogen mix calculation"
sm mix --n 4.2 --area 3 --target 170
sm mix --field "North 12" --batch "Pit A March"

section "plan the season"
sm plan add --field "North 12" --batch "Pit A March" --storage "Lagoon 1" --date "$MAR"
sm plan add --field "South 3" --batch "Lagoon B April" --storage "Slurry pit" --date "$APR" --volume 60
sm plan add --field "Riverside 7" --batch "Pit A March" --storage "Lagoon 1" --date "$MAY" --volume 120
sm plan

section "complete the first spread (storage is drawn down in the same write)"
sm plan done --id 1
sm plan
sm storage

section "the season overview"
sm status

section "hand the numbers to a spreadsheet"
sm export --out "$REPORTS"
cat "$REPORTS/plan.csv"
cat "$REPORTS/storage.csv"

section "wizard (blank lines accept every default)"
printf '\n\n\n\n\n\n\n\n' | sm wizard

section "machine-readable status for scripting"
sm status --json | sed -n '1,12p'

printf '\nDemo finished. Everything above ran against %s, which has been removed.\n' "$STATE"
printf 'Repeat any step with: %s slurrymix.py --state ./slurrymix.json <command>\n' "$PY"
