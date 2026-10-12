"""The daily status report: once a day (report.time, 09:00) one Telegram message per camera with what it saw
in the last 24 hours, how its classification and uploads went, and its health.

cmo_watch.py records what happens in local/<me_group>/history.jsonl, one JSON line each (kept
report.history_days days), so the report covers the whole day across restarts:

  {"t": 1791800000.0, "kind": "start"}                       cmo_watch started
  {"t": ..., "kind": "event", "result": "match", "labels": ["cat", "animal"], "seconds": 3.1,
   "prompt_tokens": 1821}                                     result: match, unclassified, no_target, dropped, short
  {"t": ..., "kind": "camera_restart", "stall": false}        ffmpeg restarted (the camera dropped off)
  {"t": ..., "kind": "outage", "check": "the camera", "start": ..., "end": ...}   a health problem (health.py)
  {"t": ..., "kind": "announce", "ok": ["bedroom"], "failed": ["study"]}           Google Home
  {"t": ..., "kind": "report"}                                the daily report went out ("report_asked": one
                                                              asked for with SIGUSR1)
  lines with "backfill": "log" or "server" were added from the logs and cmo_server, from before
  history.jsonl existed

Print today's report without sending it:  python3 cmo_watch.py pet_door.properties --report
Make the running service send one now (the 09:00 one still comes):
  sudo systemctl kill --kill-whom=main -s USR1 cmo_watch_pet_door
  systemctl --user kill --kill-whom=main -s USR1 cmo_watch_porch_cam
"""
import json
import logging
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("cmo")

LABEL_ORDER = ("raccoon", "cat", "dog", "animal", "person", "legs")
ICONS = {"raccoon": "🦝", "cat": "🐈", "dog": "🐕", "animal": "🐾", "person": "🧍", "legs": "🦵"}
RETRY_SECONDS = 300             # a report Telegram didn't take is tried again this often
LATE_HOURS = 12                 # a report missed (cmo_watch stopped, Telegram down) is still sent this late


class History:
    """local/<me_group>/history.jsonl: append from any thread, read for the report."""

    def __init__(self, path, keep_days=31):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days
        self.lock = threading.Lock()

    def record(self, kind, **fields):
        line = json.dumps(dict({"t": round(time.time(), 1), "kind": kind}, **fields))
        try:
            with self.lock, open(str(self.path), "a") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning("history: %s", e)

    def read(self, since=0.0):
        out = []
        try:
            with self.lock, open(str(self.path)) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("t", 0) >= since:
                        out.append(r)
        except FileNotFoundError:
            pass
        return out

    def prune(self):
        """Drop lines older than keep_days."""
        cutoff = time.time() - self.keep_days * 86400
        with self.lock:
            try:
                lines = self.path.read_text().splitlines()
            except FileNotFoundError:
                return
            keep = [l for l in lines if _t(l) >= cutoff]
            if len(keep) < len(lines):
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(l + "\n" for l in keep))
                tmp.replace(self.path)

    def last(self, kind):
        """The newest record of kind, or None."""
        found = [r for r in self.read() if r["kind"] == kind]
        return found[-1] if found else None


def _t(line):
    try:
        return json.loads(line).get("t", 0)
    except ValueError:
        return 0


# ---- the report ---------------------------------------------------------------
def ago(t, now):
    """When t was, for 'last seen': '03:12', 'yesterday 23:40', 'Oct 3 (9 days ago)'."""
    d, n = datetime.fromtimestamp(t), datetime.fromtimestamp(now)
    if d.date() == n.date():
        return d.strftime("%H:%M")
    if d.date() == (n - timedelta(days=1)).date():
        return d.strftime("yesterday %H:%M")
    return "{} ({} days ago)".format(d.strftime("%b %-d"), (n.date() - d.date()).days)


def minutes(seconds):
    m = int(round(seconds / 60))
    return "{} min".format(m) if m < 120 else "{:.1f} h".format(m / 60)


def code_version(here):
    """The cmo_client commit running, e.g. '403d1f8'."""
    try:
        r = subprocess.run(["git", "-C", str(here), "log", "-1", "--format=%h"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=10)
        return r.stdout.decode().strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def build(name, history, since, now, health_now=None, pending_uploads=None, events_dir=None, server_free=None,
          version="?", started=None):
    """The report text for [since, now). health_now: [(what, None or (down_since, detail))] from the running
    health monitor, or None when it isn't known (a --report run)."""
    rows = history.read(since)
    everything = history.read()
    events = [r for r in rows if r["kind"] == "event"]
    by = {k: [r for r in events if r.get("result") == k] for k in ("match", "unclassified", "no_target", "dropped",
                                                                  "short")}
    lines = ["📊 {} daily report ({})".format(name, socket.gethostname()),
             "{} → {}".format(datetime.fromtimestamp(since).strftime("%b %-d %H:%M"),
                              datetime.fromtimestamp(now).strftime("%b %-d %H:%M")), ""]

    # what was seen
    counts = {k: sum(1 for r in by["match"] if k in r.get("labels", [])) for k in LABEL_ORDER}
    seen = ["{} {} {}".format(ICONS[k], k, counts[k]) for k in LABEL_ORDER]
    lines.append("Seen: " + " · ".join(seen))
    last = []
    for k in ("raccoon", "cat", "dog"):
        hits = [r["t"] for r in everything if r["kind"] == "event" and r.get("result") == "match"
                and k in r.get("labels", [])]
        last.append("{} {}".format(k, ago(max(hits), now) if hits else "not in {} days".format(history.keep_days)))
    lines.append("Last seen: " + " · ".join(last))
    lines.append("")

    # events and classification
    lines.append("Motion events: {} → {} saved ({} matched, {} uncategorized), {} nothing there, {} too short{}".format(
        len(events), len(by["match"]) + len(by["unclassified"]), len(by["match"]), len(by["unclassified"]),
        len(by["no_target"]), len(by["short"]),
        ", {} dropped (Ollama failed)".format(len(by["dropped"])) if by["dropped"] else ""))
    classified = [r for r in events if r.get("seconds") is not None]
    if classified:
        secs = sorted(r["seconds"] for r in classified)
        tokens = [r["prompt_tokens"] for r in classified if r.get("prompt_tokens")]
        lines.append("Ollama: {} events, {:.1f} s each on average (slowest {:.1f} s){}".format(
            len(classified), sum(secs) / len(secs), secs[-1],
            ", {:.0f} prompt tokens avg".format(sum(tokens) / len(tokens)) if tokens else ""))
    if events:
        hours = {}
        for r in events:
            h = datetime.fromtimestamp(r["t"]).hour
            hours[h] = hours.get(h, 0) + 1
        busiest = max(hours, key=hours.get)
        lines.append("Busiest hour: {:02d}:00–{:02d}:00 ({} events)".format(busiest, (busiest + 1) % 24, hours[busiest]))
    announces = [r for r in rows if r["kind"] == "announce"]
    if announces:
        failed = sorted({d for r in announces for d in r.get("failed", [])})
        lines.append("Google Home: {} announcement{}{}".format(
            len(announces), "" if len(announces) == 1 else "s", "; speakers that failed: " + ", ".join(failed) if failed else ", every speaker answered"))
    lines.append("")

    # health
    if health_now is not None:
        down = [(what, s) for what, s in health_now if s]
        if down:
            for what, (down_since, detail) in down:
                lines.append("⚠️ {} is down since {}: {}".format(what, ago(down_since, now), detail))
        else:
            lines.append("✅ Health: everything working ({})".format(", ".join(w for w, _ in health_now)))
    outages = [r for r in rows if r["kind"] == "outage"]
    if outages:
        per = {}
        for r in outages:
            n, total = per.get(r["check"], (0, 0.0))
            per[r["check"]] = (n + 1, total + r["end"] - r["start"])
        lines.append("Problems in the last 24 h: " + "; ".join(
            "{} down {}× ({})".format(k, n, minutes(total)) for k, (n, total) in per.items()))
    else:
        lines.append("Problems in the last 24 h: none")
    restarts = [r for r in rows if r["kind"] == "camera_restart"]
    if restarts:
        lines.append("Camera reconnects: {} (short dropouts, no alert)".format(len(restarts)))

    # uploads, disks, the program
    extra = []
    if pending_uploads is not None:
        extra.append("waiting to upload: {}".format(pending_uploads))
    if events_dir:
        extra.append("disk free here: {:.1f} GB".format(shutil.disk_usage(str(events_dir)).free / 1e9))
    if server_free is not None:
        extra.append("on cmo_server: {:.1f} GB".format(server_free / 1e9))
    if extra:
        text = " · ".join(extra)
        lines.append(text[0].upper() + text[1:])
    starts = sum(1 for r in rows if r["kind"] == "start")
    lines.append("cmo_watch {}{}{}".format(version, ", running since {}".format(ago(started, now)) if started else "",
                                          ", started {}× in the last 24 h".format(starts) if starts > 1 else ""))
    return "\n".join(lines)


class DailyReporter(threading.Thread):
    """Sends the report at report_time every day. A report missed while cmo_watch was stopped is sent when it
    starts again, and one Telegram can't take is tried again, up to LATE_HOURS after report_time."""

    def __init__(self, name, history, report_time, make_report, notifier=None, stop=None):
        super().__init__(name="daily_report", daemon=True)
        self.name_, self.history = name, history
        self.hour, self.minute = (int(x) for x in report_time.split(":"))
        self.make_report = make_report      # callable(since, now) -> text
        self.notifier = notifier
        self.stop_event = stop or threading.Event()
        self.wake = threading.Event()

    def due_time(self, now):
        """The newest report time at or before now."""
        d = datetime.fromtimestamp(now).replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if d.timestamp() > now:
            d -= timedelta(days=1)
        return d.timestamp()

    def run(self):
        while not self.stop_event.is_set():
            now = time.time()
            due = self.due_time(now)
            last = self.history.last("report")
            starts = [r["t"] for r in self.history.read() if r["kind"] == "start"]
            # never sent: the first report is the next one after cmo_watch first started
            last_t = last["t"] if last else (starts[0] if starts else now)
            if last_t < due and now - due < LATE_HOURS * 3600:
                self.send(due - 86400, now)
                continue
            if last_t < due:                         # too late for that one: wait for the next
                self.history.record("report", skipped=True)
            # sleep until the next report time, or a request for one now (SIGUSR1, request())
            end = min(due + 86400, time.time() + 3600)
            while not self.stop_event.is_set() and time.time() < end:
                if self.wake.wait(min(5, max(end - time.time(), 0.1))):
                    self.wake.clear()
                    now = time.time()
                    self.send(now - 86400, now, scheduled=False)

    def request(self):
        """Send a report for the last 24 hours now, as well as the daily ones (from a signal handler)."""
        self.wake.set()

    def send(self, since, now, scheduled=True):
        try:
            text = self.make_report(since, now)
        except Exception:
            log.exception("daily report failed")
            text = "📊 {}: the daily report could not be made, see the log".format(self.name_)
        log.info("daily report%s:\n%s", "" if scheduled else " (asked for)", text)
        try:
            if self.notifier:
                self.notifier.send_text(text)
            # a report asked for is recorded apart, so the daily one still comes at report_time
            self.history.record("report" if scheduled else "report_asked")
            self.history.prune()
        except Exception as e:
            log.warning("daily report: telegram failed%s: %s",
                        " (trying again in {} min)".format(RETRY_SECONDS // 60) if scheduled else "", e)
            if scheduled:
                self.stop_event.wait(RETRY_SECONDS)
