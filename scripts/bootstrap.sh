#!/usr/bin/env bash
# One-shot server setup. Safe to re-run: existing secrets and keys in .env are kept.
#
#   1. .env: generates every missing secret
#   2. fetches the Attendee engine and builds both images
#   3. starts Postgres/Redis/SeaweedFS, migrates Attendee, creates its storage bucket
#   4. creates the Attendee admin + API key + webhook secret and writes them into .env
#   5. starts the whole stack
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m→ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
command -v openssl >/dev/null || die "openssl is required to generate secrets"
if [ "$(uname -m)" != "x86_64" ]; then
  echo "! $(uname -m) host: Attendee's image is linux/amd64 only and will run under emulation (slow)." >&2
fi

[ -f .env ] || { cp .env.example .env; chmod 600 .env; say "created .env from .env.example"; }

get_env() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    # '|' never appears in generated values (hex, base64, alphanumerics).
    sed -i.bak "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
ensure() { [ -n "$(get_env "$1")" ] || set_env "$1" "$2"; }
hex() { openssl rand -hex "$1"; }
fernet_key() { openssl rand -base64 32 | tr '+/' '-_'; }

say "secrets"
ensure POSTGRES_PASSWORD "$(hex 24)"
ensure ATTENDEE_DJANGO_SECRET_KEY "$(hex 32)"
ensure ATTENDEE_CREDENTIALS_ENCRYPTION_KEY "$(fernet_key)"
ensure ATTENDEE_ADMIN_PASSWORD "$(hex 12)"
ensure MEETBOT_SECRET_KEY "$(hex 32)"
ensure MEETBOT_API_TOKEN "$(hex 24)"
ensure MEETBOT_RESULT_WEBHOOK_SECRET "$(hex 24)"
ensure SABAH_DB_PASSWORD "$(hex 20)"
[ -n "$(get_env ATTENDEE_ADMIN_EMAIL)" ] || die "set ATTENDEE_ADMIN_EMAIL in .env (the Attendee dashboard login), then re-run"
[ -n "$(get_env ANTHROPIC_API_KEY)" ] || echo "! ANTHROPIC_API_KEY is empty — bots will join and record, but summaries will fail until you set it" >&2
provider="$(get_env MEETBOT_TRANSCRIPTION_PROVIDER)"; provider="${provider:-gladia}"
if [ "$provider" != "captions" ]; then
  provider_key="$(printf '%s' "$provider" | tr '[:lower:]' '[:upper:]')_API_KEY"
  [ -n "$(get_env "$provider_key")" ] || echo "! $provider_key is empty — bots will join, but no speech will be transcribed until you set it and run: make stt-keys" >&2
fi

if [ "${1:-}" = "--keys-only" ]; then
  docker compose up -d --wait postgres redis
else
say "Attendee engine"
ATTENDEE_REF="$(get_env ATTENDEE_REF)" ./scripts/fetch-attendee.sh

say "building images (the first Attendee build takes ~5-10 minutes)"
docker compose build

say "infrastructure"
docker compose up -d --wait postgres redis seaweedfs
# postgres-init.sh only runs on an empty volume; make sure Attendee's database exists either way.
if ! docker compose exec -T postgres psql -U meetbot -d meetbot -Atc "SELECT 1 FROM pg_database WHERE datname = 'attendee'" | grep -q 1; then
  docker compose exec -T postgres createdb -U meetbot attendee
fi
docker compose run --rm attendee-app python manage.py migrate --noinput
docker compose run --rm attendee-storage-init
fi

create_key=0
if [ -z "$(get_env ATTENDEE_API_KEY)" ] || [ -z "$(get_env ATTENDEE_WEBHOOK_SECRET)" ]; then create_key=1; fi
say "Attendee admin account, speech-to-text keys$( [ $create_key = 1 ] && echo ', API key')"
output="$(docker compose run --rm -T \
  -e ATTENDEE_ADMIN_EMAIL="$(get_env ATTENDEE_ADMIN_EMAIL)" \
  -e ATTENDEE_ADMIN_PASSWORD="$(get_env ATTENDEE_ADMIN_PASSWORD)" \
  -e GLADIA_API_KEY="$(get_env GLADIA_API_KEY)" \
  -e ELEVENLABS_API_KEY="$(get_env ELEVENLABS_API_KEY)" \
  -e OPENAI_API_KEY="$(get_env OPENAI_API_KEY)" \
  -e DEEPGRAM_API_KEY="$(get_env DEEPGRAM_API_KEY)" \
  -e CREATE_API_KEY="$create_key" \
  attendee-app python manage.py shell < scripts/bootstrap_attendee.py)"
printf '%s\n' "$output" | grep -q '^ATTENDEE_BOOTSTRAP_OK=1' || { printf '%s\n' "$output"; die "Attendee bootstrap failed"; }
printf '%s\n' "$output" | grep '^STT_KEY_UPDATED=' | sed 's/^STT_KEY_UPDATED=/  stored in Attendee: /' || true
if [ "$create_key" = 1 ]; then
  key="$(printf '%s\n' "$output" | grep '^ATTENDEE_API_KEY=' | cut -d= -f2-)"
  secret="$(printf '%s\n' "$output" | grep '^ATTENDEE_WEBHOOK_SECRET=' | cut -d= -f2-)"
  [ -n "$key" ] && [ -n "$secret" ] || die "could not create the Attendee API key"
  set_env ATTENDEE_API_KEY "$key"
  set_env ATTENDEE_WEBHOOK_SECRET "$secret"
fi
[ "${1:-}" = "--keys-only" ] && { say "done (keys synced)"; exit 0; }

say "starting the stack"
docker compose up -d
docker compose ps

cat <<EOF

✓ Meetbot is running.
  • Meetbot admin/API : $(get_env MEETBOT_PUBLIC_URL)  → 127.0.0.1:$(get_env MEETBOT_PORT)  (create a login: make superuser)
  • Attendee dashboard: https://$(get_env ATTENDEE_DOMAIN) → 127.0.0.1:$(get_env ATTENDEE_PORT)  (login: $(get_env ATTENDEE_ADMIN_EMAIL), password in .env)
  • API token          : MEETBOT_API_TOKEN in .env
Next: nginx (deploy/nginx/meetbot.sabahos.com.conf) and the SABAH.OS read-only role (make sabah-reader-sql).
EOF
