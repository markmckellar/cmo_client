#!/usr/bin/env python3
"""Say something on the Google Home speakers, from the command line.

  ./broadcast.py cat                          # the message set for "cat" (google_home.message.cat)
  ./broadcast.py raccoon                      # the message set for "raccoon"
  ./broadcast.py "Dinner is ready"            # any text
  ./broadcast.py -d bedroom -d upstairs "Hi"  # only these speakers (names from google_home.devices, or addresses)
  ./broadcast.py --list                       # the speakers, and whether each one answers (no sound)
  ./broadcast.py -n raccoon                   # dry run: show what would be said where, without a sound

A word that is a label with a message (cat, raccoon, ...) is replaced by that message, like the old
~/bin/broadcast.bash; use --text to say the word itself. The speakers, the messages and the voice's
language come from the google_home.* settings in pet_door.properties (or another camera's, --config).
A speaker that doesn't answer is skipped; the exit code is 1 if any of them failed.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import google_home  # noqa: E402
import cmo_watch    # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("message", nargs="*", help="a label (cat, raccoon, ...) or the text to say")
    ap.add_argument("-d", "--device", action="append", metavar="NAME",
                    help="only this speaker: a name from google_home.devices, or an address (repeatable)")
    ap.add_argument("-l", "--list", action="store_true", help="list the speakers and check each one (no sound)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would be said where, without a sound")
    ap.add_argument("-t", "--text", action="store_true", help="say the words as written, even if one is a label")
    ap.add_argument("--language", help="language of the voice (default google_home.language, e.g. en)")
    ap.add_argument("--timeout", type=float, default=10, help="seconds to wait for each speaker (default 10)")
    ap.add_argument("--config", default=str(HERE / "pet_door.properties"),
                    help="settings file (default %(default)s)")
    a = ap.parse_args(argv)

    gh = cmo_watch.load_config(a.config)["google_home"]
    devices = google_home.parse_devices(gh["devices"])
    if a.device:
        devices = {d: devices.get(d, d) for d in a.device}
    if not devices:
        sys.exit("error: no speakers: set google_home.devices in {} or use --device".format(a.config))

    if a.list:
        return list_devices(devices, a.timeout)

    words = " ".join(a.message).strip()
    if not words:
        ap.error("say what? give a label (cat, raccoon, ...) or some text")
    announcer = google_home.Announcer(devices, gh["message"])
    text = words if a.text else (announcer.text_for([words.lower()]) or words)
    language = a.language or gh["language"]
    try:
        url = google_home.tts_url(text, language)
    except ValueError as e:
        sys.exit("error: " + str(e))

    if a.dry_run:
        print("would say: {}".format(text))
        print("on:        {}".format(", ".join("{} ({})".format(n, h) for n, h in devices.items())))
        print("audio:     {}".format(url))
        return 0
    results = google_home.say(devices, text, language, a.timeout)
    for name, err in results.items():
        print("{:12} {:15} {}".format(name, devices[name], "said: " + text if not err else "FAILED " + err))
    return 1 if any(results.values()) else 0


def list_devices(devices, timeout):
    bad = 0
    for name, host in devices.items():
        try:
            with google_home.CastConnection(host, timeout) as cast:
                st = cast.status()
            vol = st.get("volume", {})
            apps = ", ".join(x.get("displayName", "?") for x in st.get("applications", [])) or "idle"
            print("{:12} {:15} ok      volume {:.0%}{}  {}".format(
                name, host, vol.get("level", 0), " (muted)" if vol.get("muted") else "", apps))
        except Exception as e:
            bad += 1
            print("{:12} {:15} FAILED  {}".format(name, host, e))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
