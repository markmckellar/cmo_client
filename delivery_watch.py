"""Porch deliveries: decides whether each visit to the front door was a delivery, says so on the Google Home
speakers, and labels the visit's events "delivery" (delivery.* settings, porch camera only).

cmo_watch's worker hands over every event it saves (add). Back-to-back events, each starting within
gap_seconds of the previous one's movie ending, are one visit, because one visit is often cut into two clips.
A visit is over when gap_seconds have passed since its last movie ended and the camera has no event running
or waiting that started in that time; then cmo_ollama's delivery.py decides:
  - each event with a person (or legs) and peak_motion >= min_peak_motion gets delivery.check_clip: the
    person finder and the package question (what people hold, a delivery uniform), about 30 s of GPU;
  - if one of them showed a package, the visit's other person events get the person finder too, and it is a
    delivery when the person made a round trip: up the path, to the door, back down the path.
A delivery: message said on google_home.devices (the same speakers as google_home, whether or not
google_home.enabled is on), "delivery": true added to the person events' me_ollama.json, the check's result
added to their me_data.json (cmo_watch.delivery), and their upload markers removed, so the uploader sends
them again and the server's label index has them under "delivery".
Every check is logged ("delivery: ..."), delivery or not.
"""
import json
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("cmo")


class Clip:
    def __init__(self, name, folder, start, end, labels, peak):
        self.name, self.folder = name, Path(folder)
        self.start, self.end = start, end           # wall times: first motion, end of the movie
        self.labels, self.peak = list(labels), peak
        self.result = None                          # delivery.check_clip's answer, or None
        self.checked = False

    @property
    def person(self):
        return "person" in self.labels or "legs" in self.labels

    @property
    def movie(self):
        return self.folder / "me_movie.mp4"


class DeliveryWatch(threading.Thread):
    def __init__(self, conf, model, delivery, announcer=None, uploader=None, busy_since=None):
        """conf: the delivery.* settings. delivery: cmo_ollama's delivery module. busy_since: a callable that
        returns the start (wall time) of the oldest event the camera is still recording or the worker hasn't
        handed over yet, or None."""
        super().__init__(name="delivery", daemon=True)
        self.c, self.model, self.delivery = conf, model, delivery
        self.announcer, self.uploader = announcer, uploader
        self.busy_since = busy_since or (lambda: None)
        self.q = queue.Queue()
        self.visit = []
        self.stop_event = threading.Event()
        self.stats = {"visits": 0, "checked": 0, "deliveries": 0, "failed": 0}

    def add(self, name, folder, start, end, labels, peak):
        """From the worker: an event it saved (any labels)."""
        self.q.put(Clip(name, folder, start, end, labels, peak))

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                clip = self.q.get(timeout=2)
            except queue.Empty:
                clip = None
            try:
                if clip is not None:
                    if self.visit and clip.start - self.visit[-1].end > self.c["gap_seconds"]:
                        self.close()
                    self.visit.append(clip)
                    if clip.person and clip.peak >= self.c["min_peak_motion"]:
                        self.check(clip)
                if self.visit and self.q.empty() and self.over():
                    self.close()
            except Exception:
                log.exception("delivery: check failed")
                self.visit = []

    def over(self):
        """The visit's gap has passed and nothing that started in it is still being recorded or classified."""
        edge = self.visit[-1].end + self.c["gap_seconds"]
        if time.time() < edge:
            return False
        busy = self.busy_since()
        return busy is None or busy > edge

    def check(self, clip):
        t0 = time.monotonic()
        clip.result = self.delivery.check_clip(str(clip.movie), self.model)
        clip.checked = True
        self.stats["checked"] += 1
        if clip.result is None:
            self.stats["failed"] += 1
            log.warning("delivery: %s: no answer from cmo_ollama (%.0fs)", clip.name, time.monotonic() - t0)
            return
        r = clip.result
        log.info("delivery: %s: package %s (moving frames: %s; walk-up frames: %s), person heights %s (%.0fs)",
                 clip.name, "yes" if r["package"] else "no", self.summary(r["motion_answer"]),
                 self.summary(r["crop_answer"]), r["heights"], time.monotonic() - t0)

    @staticmethod
    def summary(answer):
        if not answer:
            return "no answer"
        items = answer.get("carrying") or []
        return "{}{}".format(", ".join(map(str, items)) or "nothing", ", uniform" if answer.get("uniform") else "")

    def close(self):
        clips, self.visit = self.visit, []
        people = [c for c in clips if c.person]
        if not any(c.result and c.result["package"] for c in people):
            if any(c.checked for c in people):
                log.info("delivery: visit %s: no package, not a delivery", self.names(clips))
            return
        self.stats["visits"] += 1
        for c in people:                            # the round trip needs every person clip of the visit
            if not c.checked:
                self.check(c)
        answer = dict(self.delivery.decide([c.result for c in people]), visit=[c.name for c in clips])
        log.info("delivery: visit %s: package yes, round trip %s (person heights first/biggest/last %s) -> %s",
                 self.names(clips), "yes" if answer["round_trip"] else "no", answer["heights"],
                 "DELIVERY" if answer["delivery"] else "not a delivery")
        if not answer["delivery"]:
            return
        self.stats["deliveries"] += 1
        if self.announcer and not self.in_hours(getattr(self.announcer, "say_hours", None)):
            log.info("delivery: %s: not said, outside delivery.say_hours", clips[0].name)
        elif self.announcer:
            said = self.announcer.announce(["delivery"], clips[0].name)
            if not said:
                log.info("delivery: %s: not said (quiet after the last delivery)", clips[0].name)
        for c in people:
            self.label(c, answer)
        if self.uploader:
            self.uploader.poke()

    @staticmethod
    def in_hours(hours, now=None):
        """True when now is inside (start, end) minutes after midnight (google_home.parse_hours), or no hours."""
        if not hours:
            return True
        now = now or datetime.now()
        m, (start, end) = now.hour * 60 + now.minute, hours
        return start <= m < end if start < end else m >= start or m < end

    @staticmethod
    def names(clips):
        return "+".join(c.name for c in clips)

    def label(self, clip, answer):
        """Add delivery to the event's labels and have it uploaded again."""
        f = clip.folder
        if not f.is_dir():
            log.warning("delivery: %s: event folder is gone, not labelled", clip.name)
            return
        try:
            ollama = json.loads((f / "me_ollama.json").read_text())
            ollama["delivery"] = True
            data = json.loads((f / "me_data.json").read_text())
            cw = data.setdefault("cmo_watch", {})
            cw["labels"] = sorted(set(cw.get("labels", [])) | {"delivery"}, key=lambda l: (l != "delivery", l))
            cw["delivery"] = {**answer, "clip": None if not clip.result else {
                                  "package": clip.result["package"], "heights": clip.result["heights"],
                                  "motion_answer": clip.result["motion_answer"],
                                  "crop_answer": clip.result["crop_answer"]}}
            for name, obj in (("me_ollama.json", ollama), ("me_data.json", data)):
                tmp = f / ("." + name + ".tmp")
                tmp.write_text(json.dumps(obj, indent=4 if name == "me_ollama.json" else 2))
                tmp.replace(f / name)
            for sent in f.glob(".sent_*"):
                sent.unlink()
            log.info("delivery: %s labelled delivery, uploading it again", clip.name)
        except (OSError, ValueError) as e:
            log.warning("delivery: %s: could not label it: %s", clip.name, e)
