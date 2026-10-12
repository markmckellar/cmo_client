"""Telegram alerts for camera events, sent with telegram-send (https://github.com/rahiel/telegram-send).

Each event is one message in the format cmo_alerts used to send from the server: the event's
me_rep_image.jpg as a photo, with an HTML caption holding the time, a link to the web client, a link to
the movie on the cmo_server, and what the model saw:

  2026-10-11 20:13:17-MDT <a>visit-cmo-home</a> OR <a>watch-movie</a> cat,animal

A raccoon also gets raccoon_alert_count extra "RACCOON ALERT!!!" messages.

The uploader (cmo_upload.py) calls send() once the event is on the cmo_server, so the movie link works.
telegram-send must be configured first (telegram-send --configure), see README.md.
"""
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("cmo")

DEFAULT_CONF = Path("~/.config/telegram-send.conf").expanduser()
LABELS = ("person", "legs", "cat", "dog", "raccoon", "animal")      # cmo_alerts' order


def find_telegram_send(path=None):
    """The telegram-send program: path, else telegram-send on the PATH, else ~/.local/bin/telegram-send
    (where it is installed on the Pi, which a service's PATH doesn't include)."""
    if path:
        return os.path.expanduser(path)
    local = Path("~/.local/bin/telegram-send").expanduser()
    return shutil.which("telegram-send") or (str(local) if local.exists() else None)


def observed(event_dir):
    """The labels set in an event's me_ollama.json, in cmo_alerts' order: ['cat', 'animal']."""
    try:
        answer = json.loads((Path(event_dir) / "me_ollama.json").read_text())
    except (OSError, ValueError):
        return []
    return [k for k in LABELS if answer.get(k) is True]


def caption(event_dir, me_group, server):
    """The alert caption for an event folder named <yyyyMMdd_HHmmss_NNNN>, on server (URL ending in /)."""
    name = Path(event_dir).name
    start = datetime.strptime(name[:15], "%Y%m%d_%H%M%S")
    when = start.strftime("%Y-%m-%d %H:%M:%S")
    zone = start.astimezone().strftime("%Z")
    home = server + "cmo_client/index.html"
    movie = "{}cmo_client/movie.html?movie_path={}&time={}".format(
        server, quote("/catcam/data/{}/{}/{}/me_movie.mp4".format(me_group, name[:8], name)), quote(when))
    labels = observed(event_dir)
    return '{}-{} <a href="{}">visit-cmo-home</a> OR <a href="{}">watch-movie</a> {}'.format(
        when, zone, home, movie, ",".join(labels) or "unclassified")


class TelegramNotifier:
    def __init__(self, me_group, program=None, conf=None, raccoon_alert_count=10, timeout=60):
        self.me_group = me_group
        self.program = find_telegram_send(program)
        self.conf = os.path.expanduser(conf) if conf else None
        self.raccoon_alert_count = raccoon_alert_count
        self.timeout = timeout
        self.stats = {"sent": 0, "failed": 0}

    def check(self):
        """An error message if telegram-send can't be used, else None."""
        if not self.program:
            return "telegram-send not found: install it, or set telegram.program"
        conf = Path(self.conf) if self.conf else DEFAULT_CONF
        if not conf.exists():
            return "telegram-send is not configured ({} missing): run telegram-send --configure".format(conf)
        return None

    def send(self, event_dir, server):
        """Send the alert for one uploaded event. Raises if telegram-send fails."""
        event_dir = Path(event_dir)
        text = caption(event_dir, self.me_group, server)
        image = event_dir / "me_rep_image.jpg"
        try:
            if image.exists():
                self.run_send("--image", str(image), "--caption", text, "--format", "html")
            else:
                self.run_send("--format", "html", text)
            if "raccoon" in observed(event_dir):
                for _ in range(self.raccoon_alert_count):
                    self.run_send("RACCOON ALERT!!!")
        except Exception:
            self.stats["failed"] += 1
            raise
        self.stats["sent"] += 1
        log.info("telegram: sent %s (%s)", event_dir.name, ",".join(observed(event_dir)) or "unclassified")

    def send_text(self, text):
        """Send a plain text message (the health alerts). Raises if telegram-send fails."""
        self.run_send(text)

    def run_send(self, *args):
        cmd = [self.program] + (["--config", self.conf] if self.conf else []) + list(args)
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).decode(errors="replace").strip()[-300:]
            raise RuntimeError("telegram-send failed ({}): {}".format(r.returncode, err))
