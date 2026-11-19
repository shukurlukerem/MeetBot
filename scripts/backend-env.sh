#!/usr/bin/env bash
# Prints the two lines each SABAH.OS backend .env needs for the Notetaker toggle.
# One Meetbot serves every environment; each backend gets its own MEETBOT_TOKEN_<NAME>.
set -euo pipefail
cd "$(dirname "$0")/.."
get_env() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

url="$(get_env MEETBOT_PUBLIC_URL)"
url="${url:-https://meetbot.sabahos.com}"
found=0
while IFS= read -r line; do
  name="${line%%=*}"; token="${line#*=}"
  [ -n "$token" ] || continue
  env_name="$(printf '%s' "${name#MEETBOT_TOKEN_}" | tr '[:upper:]' '[:lower:]')"
  case "$env_name" in
    prod) where="sabahos.com" ;;
    dev) where="dev.sabahos.com" ;;
    *) where="$env_name" ;;
  esac
  printf '# %s backend .env\nMEETBOT_API_URL=%s\nMEETBOT_API_TOKEN=%s\n\n' "$where" "$url" "$token"
  found=1
done < <(grep -E '^MEETBOT_TOKEN_[A-Z0-9_]+=' .env || true)
[ "$found" = 1 ] || { echo "no MEETBOT_TOKEN_* in .env" >&2; exit 1; }
