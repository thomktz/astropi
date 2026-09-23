#!/usr/bin/env bash
# Put the current working tree on the Pi and restart it there.
#
# The dashboard is built here rather than on the Pi: the Pi has no node,
# and cross-compiling a bundle on a four-core ARM board to save one rsync
# is not a trade worth making.
set -euo pipefail

HOST="${ASTROPI_SSH:-tkientz@astropi.local}"
REMOTE="${ASTROPI_REMOTE_DIR:-astropi}"

echo "building the dashboard"
# Not silenced. `set -e` already stops a broken build from shipping, but
# with the output thrown away it stopped *quietly*, and a deploy that
# prints one line and exits zero-ish looks exactly like one that worked.
npm --prefix web run build

echo "pushing to ${HOST}:${REMOTE}"
# `data/` is left alone: the state file lives there, and it holds the
# choices made on the rig itself - the site, and which mount is driving.
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'node_modules' --exclude 'data/frames' --exclude 'data/state.json' \
  --exclude 'data/sessions' \
  ./ "${HOST}:${REMOTE}/"

ssh "${HOST}" "cd ${REMOTE} && ~/.local/bin/uv sync --frozen -q && \
  (systemctl --user restart astropi 2>/dev/null || \
   echo 'service not installed - see deploy/astropi.service')"

echo "done: http://$(echo "${HOST}" | cut -d@ -f2):8000"
