#!/usr/bin/env bash
# One-time backfill: enqueue every existing call under a directory tree
# that doesn't already have a jobs row.
#
# Usage: ./backfill.sh /vol1/sdr-scanner/audio/loudoun
#        ./backfill.sh /home/zinger/trunk-build/audio_files/sys_1   # old misfiled tree

set -uo pipefail

ROOT="${1:?usage: backfill.sh <directory>}"
LOG=/var/log/scanner/backfill.err
COUNT=0
SKIPPED=0

esc() { printf '%s' "$1" | sed "s/'/''/g"; }

find "$ROOT" -name '*.json' | while read -r JSON; do
  BASE="${JSON%.json}"

  if   [[ -f "${BASE}.m4a" ]]; then AUDIO="${BASE}.m4a"
  elif [[ -f "${BASE}.wav" ]]; then AUDIO="${BASE}.wav"
  else
    echo "$(date -Is) no audio for ${JSON}" >>"$LOG"
    continue
  fi

  J=$(esc "$JSON")
  A=$(esc "$AUDIO")

  psql -w -h pgsql -U scanner -d scanner \
    -v ON_ERROR_STOP=1 -q \
    -c "INSERT INTO jobs (json_path, audio_path)
        VALUES ('$J', '$A')
        ON CONFLICT (json_path) DO NOTHING;" \
    >/dev/null 2>>"$LOG"
done

echo "backfill of ${ROOT} complete, see ${LOG} for any errors"
