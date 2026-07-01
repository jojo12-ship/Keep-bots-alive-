"""
Rainbet Strategy Bot — entry point.

Reads RAINBET_BOT_TOKEN from environment.
Runs a health-check HTTP server on PORT so the workflow runner is satisfied.

Auto-restarts on crash with exponential backoff.
"""
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")

TOKEN = re.sub(r'\s+', '', os.getenv("RAINBET_BOT_TOKEN") or "")
if not TOKEN:
    logger.error(
        "RAINBET_BOT_TOKEN is not set. "
        "Create a bot with @BotFather, then set it as a Replit secret."
    )
    sys.exit(1)

PORT = int(os.getenv("PORT", "8002"))


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Rainbet Strategy Bot is running")

    def log_message(self, *args):
        pass


def _start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    logger.info(f"Health server listening on port {PORT}")
    server.serve_forever()


t = threading.Thread(target=_start_health_server, daemon=True)
t.start()

from bot import build_app

MAX_BACKOFF = 60   # seconds

attempt = 0
while True:
    attempt += 1
    try:
        app = build_app(TOKEN)
        logger.info(f"Rainbet Strategy Bot starting... (attempt {attempt})")
        app.run_polling(drop_pending_updates=True)
        logger.warning("Polling ended unexpectedly, restarting in 5s...")
        time.sleep(5)
    except Exception as exc:
        backoff = min(MAX_BACKOFF, 5 * attempt)
        logger.error(f"Bot crashed: {exc}. Restarting in {backoff}s...")
        time.sleep(backoff)
