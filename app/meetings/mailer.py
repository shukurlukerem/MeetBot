"""Sends mail exactly like the SABAH.OS backend (services/external/ses_service.py: SESService).

AWS SES over SMTP + STARTTLS, logged in with the SES SMTP credentials, from the SES-verified
`SES_DEFAULT_EMAIL`. The environment variables have the same names as in the backend's .env.
"""

import logging
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from django.conf import settings

logger = logging.getLogger(__name__)


class MailNotConfigured(Exception):
    pass


def send_message(subject, to_addresses, text_body, html_body=None, source=None):
    host, port = settings.SES_SMTP_HOST, settings.SES_SMTP_PORT
    username, password = settings.SES_KEY_ID, settings.SES_ACCESS_SECRET
    if not (host and username and password):
        raise MailNotConfigured("SES_KEY_ID / SES_ACCESS_SECRET are not set")

    source = source or settings.MEETBOT["EMAIL_FROM"]
    name, address = parseaddr(source)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")  # titles are often Azerbaijani/Russian
    msg["From"] = formataddr((name, address), charset="utf-8") if name else address
    msg["To"] = ", ".join(to_addresses)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        if settings.SES_SMTP_USE_TLS:
            server.starttls()
            server.ehlo()
        if server.has_extn("auth"):
            server.login(username, password)
        # The envelope sender must be the bare, SES-verified address.
        server.sendmail(address, to_addresses, msg.as_string())
    logger.info("Summary mail sent to %s", to_addresses)
