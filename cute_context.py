"""Facts about right now for the Google Home cute sentence (cmo_watch's google_home.cute), so the
sentences vary: the time of day, light or dark, the weather, a holiday or the season, and the
label's visits today. Passed to cmo_ollama's cute_text as its {context}, e.g.
"it is evening on a Thursday; it is dark outside; it is 35 degrees Fahrenheit, light snow;
it is autumn; this is the cat's 4th visit today, back after 25 minutes".

The weather (and sunrise and sunset) comes from wttr.in's JSON (google_home.cute_weather_url), which
finds the place from this machine's internet address. It is fetched on its own thread at most every
cute_weather_minutes, so an announcement never waits for it; without it there is no weather and light
or dark is guessed from the clock."""

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

HOLIDAYS = {(1, 1): "New Year's Day", (2, 14): "Valentine's Day", (3, 17): "St. Patrick's Day",
            (7, 4): "the Fourth of July", (10, 31): "Halloween", (12, 24): "Christmas Eve",
            (12, 25): "Christmas Day", (12, 31): "New Year's Eve"}


def part_of_day(hour):
    for end, name in ((5, "late at night"), (8, "early morning"), (12, "morning"), (14, "around midday"),
                      (17, "afternoon"), (21, "evening")):
        if hour < end:
            return name
    return "night"


def holiday(d):
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
        return "Thanksgiving"
    return HOLIDAYS.get((d.month, d.day))


def season(d):
    return ("winter", "spring", "summer", "autumn")[(d.month % 12) // 3]


def ordinal(n):
    words = {1: "first", 2: "second", 3: "third"}
    if n in words:
        return words[n]
    return "{}{}".format(n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def ago(seconds):
    m = int(seconds / 60 + 0.5)
    if m < 60:
        return "{} minute{}".format(m, "" if m == 1 else "s")
    h = int(m / 60 + 0.5)
    return "{} hour{}".format(h, "" if h == 1 else "s")


class Weather:
    """wttr.in's current weather, refreshed on its own thread at most every max_age seconds."""

    def __init__(self, url, max_age=1800, timeout=8):
        self.url, self.max_age, self.timeout = url, max_age, timeout
        self.data = None            # {"temp_f", "desc", "sunrise", "sunset" (time), "at" (epoch)}
        self.tried = 0.0
        self.lock = threading.Lock()
        self.busy = False

    def get(self):
        """The latest weather (None when there is none, or it is over 3 * max_age old); starts a refresh
        when it is due."""
        now = time.time()
        with self.lock:
            if not self.busy and now - self.tried >= self.max_age:
                self.busy, self.tried = True, now
                threading.Thread(target=self._fetch, name="weather", daemon=True).start()
            d = self.data
        return d if d and now - d["at"] < 3 * self.max_age else None

    def _fetch(self):
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                j = json.load(r)
            c = j["current_condition"][0]
            astro = j["weather"][0]["astronomy"][0]
            data = {"temp_f": int(c["temp_F"]), "desc": c["weatherDesc"][0]["value"].strip().lower(),
                    "sunrise": datetime.strptime(astro["sunrise"], "%I:%M %p").time(),
                    "sunset": datetime.strptime(astro["sunset"], "%I:%M %p").time(), "at": time.time()}
            with self.lock:
                self.data = data
            log.info("google home: weather %s F, %s, sun %s-%s", data["temp_f"], data["desc"],
                     data["sunrise"].strftime("%H:%M"), data["sunset"].strftime("%H:%M"))
        except Exception as e:                  # noqa: BLE001 (no weather is fine)
            log.warning("google home: no weather from %s: %s", self.url, e)
        finally:
            with self.lock:
                self.busy = False


class CuteContext:
    """facts(labels) for one announcement. Counts each label's visits per day (since cmo_watch started)."""

    def __init__(self, weather_url="", weather_minutes=30):
        self.weather = Weather(weather_url, weather_minutes * 60) if weather_url else None
        if self.weather:
            self.weather.get()                  # fetch now, so the first announcement has it
        self.visits = {}                        # label: (date, count today, datetime of the last one)
        self.lock = threading.Lock()

    def facts(self, labels, now=None):
        now = now or datetime.now()
        facts = ["it is {} on a {}".format(part_of_day(now.hour), now.strftime("%A"))]
        w = self.weather.get() if self.weather else None
        if w:
            dark = not (w["sunrise"] <= now.time() < w["sunset"])
            facts.append("it is dark outside" if dark else "it is daylight")
            facts.append("it is {} degrees Fahrenheit, {}".format(w["temp_f"], w["desc"]))
        elif now.hour < 6 or now.hour >= 19:
            facts.append("it is dark outside")
        h = holiday(now.date())
        facts.append("today is " + h if h else "it is " + season(now.date()))
        with self.lock:
            for label in labels:
                day, count, last = self.visits.get(label, (None, 0, None))
                count = count + 1 if day == now.date() else 1
                text = "this is the {}'s {} visit today".format(label, ordinal(count))
                if last and timedelta(minutes=1) <= now - last < timedelta(hours=12):
                    text += ", back after " + ago((now - last).total_seconds())
                facts.append(text)
                self.visits[label] = (now.date(), count, now)
        return "; ".join(facts)
