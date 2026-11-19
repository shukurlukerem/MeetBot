"""Prepares Attendee for the meetbot bridge. Idempotent; piped into `python manage.py shell`.

- the dashboard admin account (ATTENDEE_ADMIN_EMAIL / ATTENDEE_ADMIN_PASSWORD) and its project
- speech-to-text provider keys from .env → Attendee's encrypted project credentials
  (the same thing as typing them into Settings → Credentials in the dashboard)
- with CREATE_API_KEY=1: a new API key + the project webhook secret, printed as KEY=value lines
  that scripts/bootstrap.sh writes into .env
"""

import base64
import os

from allauth.account.models import EmailAddress

from accounts.models import User
from bots.models import ApiKey, Credentials, Project, WebhookSecret

email = os.environ["ATTENDEE_ADMIN_EMAIL"].strip().lower()
password = os.environ["ATTENDEE_ADMIN_PASSWORD"]

user = User.objects.filter(email=email).first()
if user is None:
    # Attendee's pre_save hook gives a new user its own organization and project.
    user = User(email=email)
    user.set_password(password)
    user.save()
EmailAddress.objects.update_or_create(user=user, email=email, defaults={"verified": True, "primary": True})

project = Project.objects.filter(organization=user.organization).order_by("id").first()
if project is None:
    project = Project.objects.create(name="SABAH Meetbot", organization=user.organization)

STT_KEYS = {
    "GLADIA_API_KEY": Credentials.CredentialTypes.GLADIA,
    "ELEVENLABS_API_KEY": Credentials.CredentialTypes.ELEVENLABS,
    "OPENAI_API_KEY": Credentials.CredentialTypes.OPENAI,
    "DEEPGRAM_API_KEY": Credentials.CredentialTypes.DEEPGRAM,
}
for env_name, credential_type in STT_KEYS.items():
    value = os.environ.get(env_name, "").strip()
    if not value:
        continue
    record, _ = Credentials.objects.get_or_create(project=project, credential_type=credential_type)
    if (record.get_credentials() or {}).get("api_key") != value:
        record.set_credentials({"api_key": value})
        print(f"STT_KEY_UPDATED={env_name}")

if os.environ.get("CREATE_API_KEY") == "1":
    _, api_key = ApiKey.create(project=project, name="meetbot-bridge")
    secret = WebhookSecret.objects.filter(project=project).order_by("-created_at").first()
    if secret is None:
        secret = WebhookSecret.objects.create(project=project)
    print(f"ATTENDEE_PROJECT_ID={project.object_id}")
    print(f"ATTENDEE_API_KEY={api_key}")
    print(f"ATTENDEE_WEBHOOK_SECRET={base64.b64encode(secret.get_secret()).decode()}")
print("ATTENDEE_BOOTSTRAP_OK=1")
