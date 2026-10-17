"""
FTP server for camera feed: accepts FTP connections,
authenticates a single user, and stores uploaded files in ./images.
"""

import os
import json
import logging
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

# Base dir
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config", "config.json")

with open(CONFIG_FILE, "r") as f:
    cfg = json.load(f)

# Shared low-disk guard.
import sys
sys.path.insert(0, os.path.join(PROJECT_ROOT, "lib"))
from robust import have_min_free_mb  # noqa: E402
MIN_FREE_MB = int(cfg.get("launcher", {}).get("min_free_mb", 200))

# Paths
SAVE_DIR = cfg.get("dest_dir", os.path.join(PROJECT_ROOT, "images"))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(SAVE_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "ftp_server.log")

# FTP credentials from config
ftp_cfg = cfg.get("ftp", {})
FTP_USER = ftp_cfg.get("user", "camera")
FTP_PASS = ftp_cfg.get("password")
FTP_PORT = int(ftp_cfg.get("port", 21))

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class CustomFTPHandler(FTPHandler):
    permit_foreign_addresses = True

    def ftp_STOR(self, file, mode='w'):
        # Low-disk guard: refuse new uploads when the disk is nearly full so a
        # push camera can't fill it; the forwarder keeps draining the backlog.
        if not have_min_free_mb(SAVE_DIR, MIN_FREE_MB):
            logger.warning("Refusing STOR: <%dMB free on %s", MIN_FREE_MB, SAVE_DIR)
            self.respond("552 Requested file action aborted; node low on storage.")
            return None
        return super().ftp_STOR(file, mode)

    def on_connect(self):
        logger.info(f"New connection from {self.remote_ip}:{self.remote_port}")

    def on_disconnect(self):
        logger.info(f"Disconnected: {self.remote_ip}")

    def on_login(self, username):
        logger.info(f"User logged in: {username} from {self.remote_ip}")

    def on_login_failed(self, username, password):
        logger.warning(f"Failed login attempt: username={username} ip={self.remote_ip}")

    def on_file_received(self, file_path):
        logger.info(f"File received and saved: {file_path}")

    def on_incomplete_file_received(self, file_path):
        logger.warning(f"Incomplete file received (removed): {file_path}")


def main():
    if not FTP_PASS:
        logger.critical("FTP password not set in config.json")
        return

    authorizer = DummyAuthorizer()
    authorizer.add_user(FTP_USER, FTP_PASS, SAVE_DIR, perm="elradfmw")

    handler = CustomFTPHandler
    handler.authorizer = authorizer
    handler.banner = "Reolink FTP server ready."

    address = ("0.0.0.0", FTP_PORT)
    server = FTPServer(address, handler)

    server.max_cons = 256
    server.max_cons_per_ip = 10

    logger.info(f"FTP server listening on 0.0.0.0:{FTP_PORT}, saving to {SAVE_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
