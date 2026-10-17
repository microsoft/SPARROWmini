"""
SMTP server for Reolink camera feed: accepts unauthenticated SMTP on port 25, logs activity,
and saves email attachments into config.dest_dir (the same images dir every other
service uses). Only messages from SENDER_EMAIL to RECIPIENT_EMAIL are accepted;
others are rejected.
"""

import os
import json
import asyncio
import logging
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import AsyncMessage
from aiosmtpd.smtp import SMTP
from email import message_from_bytes, policy
from email.utils import parseaddr

# Base dir (same folder as this script)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config", "config.json")

# Load config so we save into the same dest_dir every other service uses
# (was hardcoded to BASE_DIR/images, which silently broke if dest_dir moved).
try:
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

# Shared low-disk guard.
import sys
sys.path.insert(0, os.path.join(PROJECT_ROOT, "lib"))
from robust import have_min_free_mb  # noqa: E402
MIN_FREE_MB = int(cfg.get("launcher", {}).get("min_free_mb", 200))

# Configuration Paths (local folders)
SAVE_DIR = cfg.get("dest_dir", os.path.join(PROJECT_ROOT, "images"))
LOG_DIR  = os.path.join(PROJECT_ROOT, "logs")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(SAVE_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "smtp_server.log")

# Setup Logging & Folders
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Default Configuration
SENDER_EMAIL = "camera@localnetwork.com"
RECIPIENT_EMAIL = "alerts@localnetwork.com"

# SMTP Server Classes
class CustomSMTP(SMTP):
    async def smtp_AUTH(self, arg):
        logger.info("Authentication skipped, allowing unauthenticated emails.")
        await self.push('235 Authentication successful')
        self.session.authenticated = True

    async def smtp_MAIL(self, arg):
        logger.info(f"MAIL FROM received: {arg}")
        self.session.authenticated = True
        await super().smtp_MAIL(arg)

    async def smtp_EHLO(self, arg):
        logger.info(f"EHLO from camera: {arg}")
        self.session.host_name = arg
        await self.push('250-10.42.0.1')
        await self.push('250-AUTH LOGIN PLAIN')
        await self.push('250 OK')

class EmailHandler(AsyncMessage):
    async def handle_message(self, message):
        logger.info("Raw message received")
        email_msg = message_from_bytes(message.as_bytes(), policy=policy.default)
        mail_from = parseaddr(email_msg['From'])[1]
        rcpt_to  = email_msg['To']
        subject  = email_msg['Subject']
        logger.info(f"From: {mail_from}, To: {rcpt_to}, Subject: {subject}")

        if mail_from != SENDER_EMAIL:
            logger.warning("Unauthorized sender: %s", mail_from)
            return '550 Unauthorized sender'
        if (rcpt_to is None) or (RECIPIENT_EMAIL not in rcpt_to):
            logger.warning("Unauthorized recipient: %s", rcpt_to)
            return '550 Unauthorized recipient'

        if not have_min_free_mb(SAVE_DIR, MIN_FREE_MB):
            logger.warning("Refusing message: <%dMB free on %s", MIN_FREE_MB, SAVE_DIR)
            return '452 Insufficient system storage'

        saved = False
        if email_msg.is_multipart():
            for part in email_msg.iter_parts():
                if part.get_content_disposition() == "attachment":
                    fn = part.get_filename()
                    if fn:
                        # SECURITY: strip any directory components so a crafted
                        # attachment name (../../config.json, absolute paths) can't
                        # escape SAVE_DIR and overwrite config/binaries.
                        fn = os.path.basename(fn)
                        payload = part.get_payload(decode=True)
                        if not fn or payload is None:
                            logger.warning("Skipping attachment with empty name/undecodable payload")
                            continue
                        path = os.path.join(SAVE_DIR, fn)
                        # Don't clobber an existing capture still waiting to be sent.
                        base, ext = os.path.splitext(path)
                        i = 1
                        while os.path.exists(path) and i < 1000:
                            path = f"{base}_{i}{ext}"
                            i += 1
                        with open(path, 'wb') as f:
                            f.write(payload)
                        logger.info(f"Saved attachment: {path}")
                        saved = True
        else:
            logger.info("No attachments found.")

        if not saved:
            logger.warning("Email received with no attachments")

        logger.info("Email handled successfully")
        return '250 OK'

class CustomController(Controller):
    def factory(self):
        return CustomSMTP(self.handler, auth_require_tls=False)

# Main
if __name__ == "__main__":
    handler = EmailHandler()
    controller = CustomController(
        handler,
        hostname='0.0.0.0',
        port=2525
    )
    controller.start()
    logger.info('SMTP server is running on port 25...')
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
