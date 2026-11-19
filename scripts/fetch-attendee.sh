#!/usr/bin/env bash
# Downloads the Attendee engine at a pinned commit into ./attendee-engine (unmodified, ELv2).
# Bump ATTENDEE_REF in .env and re-run (or `make update-attendee`) to upgrade.
set -euo pipefail
cd "$(dirname "$0")/.."

REF="${ATTENDEE_REF:-}"
if [ -z "$REF" ] && [ -f .env ]; then
  REF="$(grep -E '^ATTENDEE_REF=' .env | tail -1 | cut -d= -f2- || true)"
fi
REF="${REF:-66c30688a0b58771e359ce732390f042b57d0a6e}"
REPO="https://github.com/attendee-labs/attendee.git"

if [ ! -d attendee-engine/.git ]; then
  echo "→ cloning Attendee"
  git clone --filter=blob:none "$REPO" attendee-engine
fi

cd attendee-engine
git fetch --quiet origin
git -c advice.detachedHead=false checkout --quiet "$REF"
echo "→ Attendee at $(git rev-parse --short HEAD) ($(git log -1 --format=%cd --date=short))"
