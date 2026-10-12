"""Upload finished camera events to one or more cmo_servers, and keep them on disk until they are.

cmo_watch.py writes each event to <events_dir>/<me_name>/ (it builds the folder under a hidden name and
renames it when complete):
  me_movie.mp4       the movie
  me_rep_image.jpg   the frame that best shows the match
  me_ollama.json     {"legs": false, "person": true, ...}, for the web client's search and cmo_alerts
  me_data.json       the event, in the format the web client reads

The uploader sends each event to every active server in the order above. me_data.json goes last: the
server lists an event only once it has one, so a half-uploaded event never shows up, and cmo_alerts
finds me_ollama.json already there when it sees the event. Every file must come back HTTP 200; if one
doesn't, the event stays on disk and is tried again later, also after a restart or reboot. A server
that has the whole event gets a ".sent_<server>" marker in the folder, so it is never sent twice.
When every server has it, the folder is deleted (or kept for upload.keep_days, see the camera's .properties file).

With a notifier (telegram_notify.py, telegram.enabled), the Telegram alert goes out once the event is on
every server, so its movie link works, and before the folder is deleted. A ".telegram_sent" marker keeps
it from being sent twice; if Telegram can't be reached it is tried again every retry_seconds, up to
ALERT_TRIES times, and then given up (".telegram_failed") so the event is still cleaned up.
"""
import logging
import re
import shutil
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("cmo")

UPLOAD_ORDER = ("me_ollama.json", "me_movie.mp4", "me_rep_image.jpg", "me_data.json")
ALERT_TRIES = 5


def server_tag(url):
    """'http://173.255.215.223:9090/' -> '173.255.215.223_9090', for the .sent_ marker file name."""
    return re.sub(r"[^A-Za-z0-9.]+", "_", re.sub(r"^\w+://", "", url)).strip("_")


class CmoUploader(threading.Thread):
    """Scans the events dir every few seconds and uploads what isn't on every server yet."""

    def __init__(self, events_dir, me_group, servers, retry_seconds=60, delete_after_upload=True,
                 keep_days=3, timeout=120, stop=None, notifier=None):
        super().__init__(name="uploader", daemon=True)
        self.dir = Path(events_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.me_group = me_group
        self.servers = [s["url"].rstrip("/") + "/" for s in servers if s.get("active", True)]
        self.retry = retry_seconds
        self.delete_after = delete_after_upload
        self.keep_days = keep_days
        self.timeout = timeout
        self.stop_event = stop or threading.Event()
        self.wake = threading.Event()
        self.next_try = {}          # (event dir, server) -> monotonic time of the next attempt after a failure
        self.session = requests.Session()
        self.notifier = notifier
        self.alert_fails = {}       # event name -> failed Telegram tries
        self.stats = {"sent": 0, "failed": 0}

    def poke(self):
        """A new event is ready: scan now instead of at the next interval."""
        self.wake.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.scan()
            except Exception as e:     # keep the thread alive whatever goes wrong with one scan
                log.warning("upload scan failed: %s", e)
            self.wake.wait(10)
            self.wake.clear()

    def finish(self, timeout=60):
        """Send what is ready now (used at shutdown), giving up after timeout seconds."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.pending():
            if not self.scan(deadline=end):
                break

    def pending(self):
        return [d for d in self.ready() if self.missing(d)]

    def ready(self):
        """Complete events (not hidden, so not still being written), oldest first."""
        return sorted(d for d in self.dir.iterdir() if d.is_dir() and not d.name.startswith("."))

    def missing(self, d):
        return [s for s in self.servers if not (d / (".sent_" + server_tag(s))).exists()]

    def scan(self, deadline=None):
        """One pass over the events. Returns True if anything was sent."""
        sent_any = False
        for d in self.ready():
            if self.stop_event.is_set() and deadline is None:
                break
            for server in self.missing(d):
                if time.monotonic() < self.next_try.get((d.name, server), 0):
                    continue
                if deadline is not None and time.monotonic() > deadline:
                    return sent_any
                try:
                    self.upload(d, server)
                    (d / (".sent_" + server_tag(server))).touch()
                    self.next_try.pop((d.name, server), None)
                    self.stats["sent"] += 1
                    sent_any = True
                    log.info("uploaded %s to %s", d.name, server)
                except Exception as e:
                    self.stats["failed"] += 1
                    self.next_try[(d.name, server)] = time.monotonic() + self.retry
                    log.warning("upload of %s to %s failed (retrying in %ds): %s", d.name, server, self.retry, e)
            if not self.missing(d) and self.alerted(d):
                self.done(d)
        self.prune()
        return sent_any

    def upload(self, d, server):
        day, name = d.name[:8], d.name
        url = "{}uploadme/{}/{}/{}".format(server, self.me_group, day, name)
        for fname in UPLOAD_ORDER:
            path = d / fname
            if not path.exists():
                if fname == "me_rep_image.jpg":
                    continue
                raise FileNotFoundError(str(path))
            with open(str(path), "rb") as f:
                r = self.session.post(url, data={"image_dir": ""}, files={"file": (fname, f)}, timeout=self.timeout)
            r.raise_for_status()

    def alerted(self, d):
        """Send the event's Telegram alert if it hasn't gone yet. False while it should be tried again."""
        if not self.notifier or (d / ".telegram_sent").exists() or (d / ".telegram_failed").exists():
            return True
        if time.monotonic() < self.next_try.get((d.name, "telegram"), 0):
            return False
        try:
            self.notifier.send(d, self.servers[0])
            (d / ".telegram_sent").touch()
            return True
        except Exception as e:
            tries = self.alert_fails[d.name] = self.alert_fails.get(d.name, 0) + 1
            if tries >= ALERT_TRIES:
                (d / ".telegram_failed").touch()
                log.warning("telegram alert for %s failed %d times, giving up: %s", d.name, tries, e)
                return True
            self.next_try[(d.name, "telegram")] = time.monotonic() + self.retry
            log.warning("telegram alert for %s failed (retrying in %ds): %s", d.name, self.retry, e)
            return False

    def done(self, d):
        if self.delete_after:
            shutil.rmtree(str(d), ignore_errors=True)
            log.info("deleted %s (on every server)", d.name)

    def prune(self):
        """Uploaded events kept on disk (delete_after_upload false) go after keep_days."""
        if self.delete_after or not self.keep_days:
            return
        cutoff = time.time() - self.keep_days * 86400
        for d in self.ready():
            if not self.missing(d) and d.stat().st_mtime < cutoff:
                shutil.rmtree(str(d), ignore_errors=True)
                log.info("deleted %s (older than %s days)", d.name, self.keep_days)


def upload_dir_once(events_dir, me_group, servers, notifier=None):
    """For a manual run: send everything pending once. Returns the number of events still pending."""
    up = CmoUploader(events_dir, me_group, servers, retry_seconds=0, notifier=notifier)
    up.scan()
    return len(up.pending())


if __name__ == "__main__":
    import argparse
    import sys
    import cmo_watch                       # the same defaults, paths and servers as cmo_watch.py
    ap = argparse.ArgumentParser(description="Send a camera's pending events now (cmo_watch.py also does this).")
    ap.add_argument("config", help="the camera's settings file, e.g. pet_door.properties")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    conf = cmo_watch.load_config(a.config)
    cmo_watch.use_cmo_ollama(conf["classifier"]["cmo_ollama_dir"])
    notifier = cmo_watch.telegram_notifier(conf)
    left = upload_dir_once(conf["events_dir"], conf["me_group"], cmo_watch.upload_servers(conf), notifier)
    log.info("%d events still pending", left)
    sys.exit(1 if left else 0)
