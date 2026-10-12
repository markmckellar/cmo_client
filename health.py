"""Health checks for cmo_watch.py, with one Telegram message when something breaks and one when it is fixed.

Every health.check_seconds the monitor runs each check:

  ollama       the Ollama server answers (GET <api>/api/version, api from cmo_ollama's ollama_common.bash),
               and the last event's classification did not fail
  camera       video is arriving (a frame within the last health.check_seconds)
  cmo_server   each upload server answers /catcam/health
  server_disk  each upload server has at least health.min_free_gb free (from /catcam/health)
  disk         this machine has at least health.min_free_gb free where the events are kept

A check must fail for health.down_minutes in a row before it counts as down, so short dropouts (the porch
camera's Wi-Fi, an Ollama restart) send nothing. Then:
  - one PROBLEM message;
  - while it stays down, a reminder every health.repeat_hours (0 = never);
  - one FIXED message when it works again, saying how long it was down.
If Telegram itself can't be reached (the internet is down), the message is tried again at the next check;
if the problem is over by then, a single "was down from ... to ..." message goes instead. Everything is
also logged. Without Telegram (telegram.enabled false, or --no-upload) the messages are only logged.
"""
import logging
import shutil
import socket
import threading
import time
from datetime import datetime

import requests

log = logging.getLogger("cmo")


def duration(seconds):
    """3725 -> '1 h 2 min'."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return "{} min".format(max(minutes, 1))
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return "{} h {} min".format(hours, minutes) if minutes else "{} h".format(hours)
    return "{} days {} h".format(*divmod(hours, 24))


def clock(t):
    """A wall time as '05:50', or 'Oct 11 05:50' when it was not today."""
    d = datetime.fromtimestamp(t)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%b %d %H:%M")


UNKNOWN = object()      # a check that can't tell right now (keeps its state)


class Check:
    """One thing to watch: fn() returns None when it is fine, what is wrong (text), or UNKNOWN."""

    def __init__(self, name, what, fn):
        self.name, self.what, self.fn = name, what, fn
        self.down_since = None      # wall time of the first failure in a row
        self.detail = ""            # what was wrong, the last time
        self.alerted = False        # the PROBLEM message has gone out
        self.last_alert = 0.0       # wall time of the last PROBLEM or reminder message
        self.up_since = None        # wall time it started working again, while that message waits


class HealthMonitor(threading.Thread):
    def __init__(self, camera_name, checks, notifier=None, check_seconds=60, down_minutes=5, repeat_hours=24,
                 stop=None, history=None):
        super().__init__(name="health", daemon=True)
        self.camera_name = camera_name
        self.checks = checks
        self.notifier = notifier            # a TelegramNotifier (send_text), or None to only log
        self.interval = check_seconds
        self.grace = down_minutes * 60
        self.repeat = repeat_hours * 3600
        self.stop_event = stop or threading.Event()
        self.host = socket.gethostname()
        self.history = history              # daily.History: outages go in the daily report
        self.stats = {"problems": 0, "fixed": 0, "messages": 0, "message_failures": 0}

    def run(self):
        # let the camera and the first events start before judging anything
        self.stop_event.wait(self.interval)
        while not self.stop_event.is_set():
            self.run_checks()
            self.stop_event.wait(self.interval)

    def run_checks(self, now=None):
        for c in self.checks:
            try:
                problem = c.fn()
            except Exception as e:          # a broken check is itself a problem, not a crash
                problem = "check failed: {}".format(e)
            self.update(c, problem, now or time.time())

    def update(self, c, problem, now):
        if problem is UNKNOWN:
            return
        if problem:
            c.up_since = None
            if c.down_since is None:
                c.down_since = now
                log.warning("health: %s: %s", c.what, problem)
            elif problem != c.detail:
                log.warning("health: %s: still down: %s", c.what, problem)
            c.detail = problem
            down_for = now - c.down_since
            if not c.alerted and down_for >= self.grace:
                if self.send("⚠️ {}: {} is down since {} ({}): {}".format(
                        self.where(), c.what, clock(c.down_since), duration(down_for), problem)):
                    c.alerted, c.last_alert = True, now
                    self.stats["problems"] += 1
            elif c.alerted and self.repeat and now - c.last_alert >= self.repeat:
                if self.send("⚠️ {}: {} is still down, since {} ({}): {}".format(
                        self.where(), c.what, clock(c.down_since), duration(down_for), problem)):
                    c.last_alert = now
            return
        if c.down_since is None:
            return
        if c.up_since is None:
            c.up_since = now
            if self.history and now - c.down_since >= self.grace:
                self.history.record("outage", check=c.what, start=round(c.down_since), end=round(now),
                                    detail=c.detail)
        down_for = c.up_since - c.down_since
        if c.alerted:
            text = "✅ {}: {} is working again since {} (down {}, from {})".format(
                self.where(), c.what, clock(c.up_since), duration(down_for), clock(c.down_since))
        elif down_for >= self.grace:
            # down long enough for an alert, but Telegram couldn't be reached until now
            text = "⚠️✅ {}: {} was down from {} to {} ({}): {}".format(
                self.where(), c.what, clock(c.down_since), clock(c.up_since), duration(down_for), c.detail)
        else:
            log.info("health: %s is working again (down %.0f s, no alert)", c.what, down_for)
            c.down_since, c.detail, c.up_since = None, "", None
            return
        if self.send(text):
            self.stats["fixed"] += 1
            c.down_since, c.detail, c.alerted, c.up_since = None, "", False, None
        # else: try again at the next check (the problem stays on record until the message is out)

    def status(self):
        """[(what, None if working, else (down_since, detail))] for the daily report."""
        return [(c.what, (c.down_since, c.detail) if c.down_since and not c.up_since else None)
                for c in self.checks]

    def where(self):
        return "{} ({})".format(self.camera_name, self.host)

    def send(self, text):
        """Send text to Telegram (and log it). True when it went out, or there is no Telegram."""
        log.warning("health: %s", text) if text.startswith("⚠") else log.info("health: %s", text)
        if self.notifier is None:
            return True
        try:
            self.notifier.send_text(text)
            self.stats["messages"] += 1
            return True
        except Exception as e:
            self.stats["message_failures"] += 1
            log.warning("health: telegram message failed (trying again at the next check): %s", e)
            return False


# ---- the checks ----------------------------------------------------------------
def ollama_check(api, worker, timeout=10):
    """The Ollama server answers, and the last classification did not fail."""
    def check():
        try:
            r = requests.get(api.rstrip("/") + "/api/version", timeout=timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            return "{} not answering: {}".format(api, short(e))
        failed = worker.llm_failed_since
        if failed:
            return "{} answers, but classifying events has failed since {}".format(api, clock(failed))
        return None
    return Check("ollama", "Ollama", check)


def camera_check(camera, max_age):
    """Video has arrived within max_age seconds."""
    def check():
        age = time.monotonic() - camera.last_frame
        if age > max_age:
            return "no video from {} for {}".format(camera.name(), duration(age))
        return None
    return Check("camera", "the camera", check)


def server_checks(server, min_free_gb, timeout=20):
    """[cmo_server answers /catcam/health, and has min_free_gb free]: two checks sharing one request."""
    state = {"health": None}

    def answers():
        state["health"] = None
        try:
            r = requests.get(server + "catcam/health", timeout=timeout)
            r.raise_for_status()
            state["health"] = r.json()
        except (requests.RequestException, ValueError) as e:
            return "{} not answering: {}".format(server, short(e))
        return None

    def disk():
        free = (state["health"] or {}).get("free_bytes")
        if free is None:
            return UNKNOWN          # the server didn't answer: that is the other check's problem
        if free < min_free_gb * 1e9:
            return "{} has only {:.1f} GB free".format(server, free / 1e9)
        return None
    return [Check("cmo_server", "cmo_server " + server, answers),
            Check("server_disk", "cmo_server's disk", disk)]


def disk_check(path, min_free_gb):
    """This machine has min_free_gb free where the events are kept."""
    def check():
        free = shutil.disk_usage(str(path)).free
        if free < min_free_gb * 1e9:
            return "only {:.1f} GB free for {}".format(free / 1e9, path)
        return None
    return Check("disk", "this machine's disk", check)


def short(e):
    """An exception as one short line (requests' messages can be very long)."""
    text = str(e).splitlines()[0] if str(e) else e.__class__.__name__
    for marker in ("Failed to establish a new connection: ", "Read timed out"):
        if marker in text:
            text = text[text.index(marker):].rstrip("'\")")
            break
    return text[:200]
