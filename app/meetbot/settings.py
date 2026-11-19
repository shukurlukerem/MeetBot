"""Settings for the SABAH Meetbot bridge service.

Everything is configured from the environment (see ../.env.example); nothing
secret lives in this file.
"""

import json
import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def env_json(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured(f"{name} must be valid JSON: {exc}") from exc


DEBUG = env_bool("MEETBOT_DEBUG", False)
SECRET_KEY = env("MEETBOT_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("MEETBOT_SECRET_KEY is required")
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = env_list("MEETBOT_ALLOWED_HOSTS", "*")
if "*" not in ALLOWED_HOSTS:
    # Attendee delivers webhooks to the compose service name; healthchecks use localhost.
    ALLOWED_HOSTS += ["meetbot-web", "localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = env_list("MEETBOT_CSRF_TRUSTED_ORIGINS")
# TLS is terminated by the reverse proxy in front of the stack.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env_bool("MEETBOT_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "meetings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "meetbot.urls"
WSGI_APPLICATION = "meetbot.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", "sqlite:///" + str(BASE_DIR / "db.sqlite3")),
        conn_max_age=60,
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "az"
TIME_ZONE = env("TIME_ZONE", "Asia/Baku")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
if env_bool("MEETBOT_TESTING", False):
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("MEETBOT_LOG_LEVEL", "INFO")},
    "loggers": {"httpx2": {"level": "WARNING"}, "httpx": {"level": "WARNING"}},
}

# --- Email: same AWS SES SMTP sender as the SABAH.OS backend --------------------
# Same variable names as sabah-os-web-backend/.env, so the values are copied as-is.
SES_SMTP_HOST = env("SES_SMTP_HOST") or env("SES_ENDPOINT_URL", "email-smtp.eu-central-1.amazonaws.com")
SES_SMTP_PORT = env_int("SES_SMTP_PORT", 587)
SES_SMTP_USE_TLS = env_bool("SES_SMTP_USE_TLS", True)
SES_KEY_ID = env("SES_KEY_ID")
SES_ACCESS_SECRET = env("SES_ACCESS_SECRET")
SES_DEFAULT_EMAIL = env("SES_DEFAULT_EMAIL", "no-reply@sabahhub.com")


def _sabah_database_url():
    if env("SABAH_DATABASE_URL"):
        return env("SABAH_DATABASE_URL")
    host, user = env("SABAH_DB_HOST"), env("SABAH_DB_USER")
    if not (host and user):
        return ""
    from urllib.parse import quote

    password = env("SABAH_DB_PASSWORD")
    auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
    return f"postgresql://{auth}@{host}:{env('SABAH_DB_PORT', '5432')}/{quote(env('SABAH_DB_NAME', 'sabah_os_ai_db'), safe='')}"


# --- Meetbot -----------------------------------------------------------------
def _api_clients():
    """`MEETBOT_TOKEN_<NAME>=<token>` → {token: "name"}; one per SABAH.OS environment (prod, dev, …)."""
    clients = {}
    for key, value in os.environ.items():
        if key.startswith("MEETBOT_TOKEN_") and value.strip():
            clients[value.strip()] = key[len("MEETBOT_TOKEN_"):].lower()
    return clients


MEETBOT = {
    # Admin token for our own REST API (Authorization: Bearer <token>): sees every client.
    "API_TOKEN": env("MEETBOT_API_TOKEN"),
    # Per-environment tokens. Each SABAH.OS backend (sabahos.com, dev.sabahos.com) uses its own,
    # and only ever sees its own meetings — one Meetbot serves them all.
    "API_CLIENTS": _api_clients(),
    "PUBLIC_URL": env("MEETBOT_PUBLIC_URL").rstrip("/"),
    # Attendee engine
    "ATTENDEE_API_URL": env("ATTENDEE_API_URL", "http://attendee-app:8000").rstrip("/"),
    "ATTENDEE_API_KEY": env("ATTENDEE_API_KEY"),
    "ATTENDEE_WEBHOOK_SECRET": env("ATTENDEE_WEBHOOK_SECRET"),
    # Where Attendee posts bot webhooks. Inside docker compose this is the
    # internal service name, so the webhook never leaves the host.
    "WEBHOOK_URL": env("MEETBOT_WEBHOOK_URL", "http://meetbot-web:8000/webhooks/attendee/"),
    # Bot behaviour
    "BOT_NAME": env("MEETBOT_BOT_NAME", "SABAH Notetaker"),
    "JOIN_MESSAGE": env(
        "MEETBOT_JOIN_MESSAGE",
        # Plain BMP text only: Attendee types it into the chat via ChromeDriver,
        # which cannot send emoji.
        "Hi, this is SABAH Notetaker. I am transcribing this meeting so everyone gets written notes afterwards.",
    ),
    # gladia | elevenlabs | openai | deepgram | captions (Google Meet's own captions, one language only)
    "TRANSCRIPTION_PROVIDER": env("MEETBOT_TRANSCRIPTION_PROVIDER", "gladia").strip().lower(),
    # Languages spoken in meetings (ISO 639-1); people may switch between them mid-meeting.
    "LANGUAGES": env_list("MEETBOT_LANGUAGES", "az,ru,en"),
    "CAPTION_LANGUAGE": env("MEETBOT_CAPTION_LANGUAGE", "az-AZ"),
    # Raw Attendee transcription_settings JSON; overrides the provider presets when set.
    "TRANSCRIPTION_SETTINGS": env_json("MEETBOT_TRANSCRIPTION_SETTINGS"),
    "RECORDING_FORMAT": env("MEETBOT_RECORDING_FORMAT", "none"),
    "MAX_MEETING_MINUTES": env_int("MEETBOT_MAX_MEETING_MINUTES", 240),
    "AUTOMATIC_LEAVE_SETTINGS": env_json("MEETBOT_AUTOMATIC_LEAVE_SETTINGS"),
    "GOOGLE_MEET_USE_LOGIN": env_bool("MEETBOT_GOOGLE_MEET_USE_LOGIN", False),
    "JOIN_EARLY_SECONDS": env_int("MEETBOT_JOIN_EARLY_SECONDS", 60),
    # Calendar source (SABAH.OS database, read-only)
    "SABAH_DATABASE_URL": _sabah_database_url(),
    "JOIN_POLICY": env("MEETBOT_JOIN_POLICY", "all"),  # all | tagged
    "JOIN_TAG": env("MEETBOT_JOIN_TAG", "#meetbot"),
    "SKIP_TAG": env("MEETBOT_SKIP_TAG", "#nobot"),
    "SKIP_PRIVATE": env_bool("MEETBOT_SKIP_PRIVATE", True),
    "ORGANIZER_ALLOWLIST": [e.lower() for e in env_list("MEETBOT_ORGANIZER_ALLOWLIST")],
    "LOOKAHEAD_MINUTES": env_int("MEETBOT_LOOKAHEAD_MINUTES", 30),
    "SYNC_INTERVAL_SECONDS": env_int("MEETBOT_SYNC_INTERVAL_SECONDS", 60),
    # Claude
    "ANTHROPIC_MODEL": env("ANTHROPIC_MODEL", "claude-opus-5-5"),
    "ANTHROPIC_EFFORT": env("ANTHROPIC_EFFORT", "high"),
    "ANTHROPIC_FALLBACKS": env_bool("ANTHROPIC_FALLBACKS", True),
    "SUMMARY_LANGUAGE": env("MEETBOT_SUMMARY_LANGUAGE", "en"),
    # Delivery
    "EMAIL_TO": env("MEETBOT_EMAIL_TO", "none"),  # none | organizer | invitees
    # Display name + the SES-verified sender address.
    "EMAIL_FROM": env("MEETBOT_EMAIL_FROM") or f"SABAH Meetbot <{SES_DEFAULT_EMAIL}>",
    "EMAIL_ALLOWED_DOMAINS": [d.lower() for d in env_list("MEETBOT_EMAIL_ALLOWED_DOMAINS")],
    "RESULT_WEBHOOK_URL": env("MEETBOT_RESULT_WEBHOOK_URL"),
    "RESULT_WEBHOOK_SECRET": env("MEETBOT_RESULT_WEBHOOK_SECRET"),
    # Worker
    "MAX_ATTEMPTS": env_int("MEETBOT_MAX_ATTEMPTS", 5),
}
