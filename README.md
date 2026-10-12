# cmo_client: the CMO camera watcher

`cmo_watch.py` watches a camera and keeps the movies that show a person, legs, cat, dog, raccoon or
other animal. It uploads them to cmo_server and sends the alerts: Telegram, and spoken messages on the
Google Home speakers. It is one program for every CMO camera. What differs between the cameras is in
each camera's settings file:

| | `pet_door.properties` | `porch_cam.properties` |
|---|---|---|
| Runs on | the pet door Pi (`picmopetdoor`, Python 3.7) | mckada (in `.venv`) |
| Video (`camera.type`) | `v4l2`: the Pi camera's own H.264 encoder, 1296x972 | `rtsp`: the Jarnex camera's 640x360 H.264 sub stream, with audio |
| Name on cmo_server (`me_group`) | `pet_door` | `porch_cam` |
| Telegram bot (`telegram.config`) | that machine's telegram-send config: @DeCatsBot | that machine's: the porch bot |
| Google Home (`google_home.*`) | cat and raccoon messages on 3 speakers | off |
| Motion | 1% of the picture, no masks | 0.4%, the camera's clock masked out |
| After upload | event deleted | kept 30 days in `local/porch_cam/events/` |
| Log | `~/logs/pet_watch.log` | `~/logs/porch_watch.log` |

Both classify with the shared scripts in `~/dev/cmo_ollama` (`call_ollama_movie.bash`), so they ask the
same question of the same model on the same Ollama server, and upload to the same cmo_server.

This replaces the `pet_cam` and `porch_cam` repos (and, before them, this repo's old OpenCV
`capture_motion/` code, which is still in the git history).

## How it works

```
camera ──► one ffmpeg ─┬─ stream copy ──► local/<me_group>/buffer/seg_<time>.mp4   4 s segments, last 5 minutes
                       └─ 4 fps RGB ────► detector (numpy motion score)
                                            └─ event ──► worker: cut the movie, ask ~/dev/cmo_ollama
                                                           ├─ match ──► Google Home, at once
                                                           └─ match ──► local/<me_group>/events/<yyyyMMdd_HHmmss_NNNN>/
                                                                          └─► uploader ──► cmo_server(s) ──► Telegram
```

- **Recording**: ffmpeg only copies the camera's own H.264 into short MP4 segments, so recording costs
  almost no CPU and the movies are real time. The same ffmpeg decodes the stream to 4 small frames a
  second for the motion check. A camera that stops sending video (`buffer.stall_seconds`) gets ffmpeg
  restarted; a network camera that drops off is reconnected the same way.
- **Events**: motion in `min_motion_frames` analysed frames opens an event; `post_roll` seconds of quiet
  (or `max_event` seconds) closes it. The movie runs from `pre_roll` seconds before the motion to
  `post_roll` after it. Events with fewer than `min_event_frames` moving frames are dropped without asking
  Ollama.
- **Classification**: cmo_ollama's `call_ollama_movie.bash` is asked about the movie (through
  `~/dev/cmo_ollama/call_ollama.py`). Of the event's `max_frames` sharpest moving frames,
  `call_ollama.bash` picks the one that shows the match for the preview picture. Events with no target
  are deleted (`save_rejects` keeps a picture of them, for tuning). If Ollama can't answer,
  `classifier.upload_unclassified` decides whether the event is uploaded anyway (all labels false,
  `"error": true`) or dropped.
- **Google Home** (`google_home.py`): right after a match, the message set for its labels
  (`google_home.message.cat`, `.raccoon`, ...) is spoken on every speaker in `google_home.devices` at once,
  on its own thread. A speaker that doesn't answer is skipped and logged.
- **Uploading** (`cmo_upload.py`): each event goes to every server in `upload.servers` (empty: the
  `cmo_server` in cmo_ollama's `ollama_common.bash`, shared by every camera): `me_ollama.json`, the movie,
  the picture, and `me_data.json` last, so a half-uploaded event never shows up. A failed upload is retried
  every `retry_seconds`, also after a restart. A ".sent_<server>" marker keeps it from going twice.
- **Telegram** (`telegram_notify.py`): once the event is on the server, its picture goes out with
  telegram-send, captioned in the format cmo_alerts used:
  `2026-10-11 20:13:17-MDT visit-cmo-home OR watch-movie cat,animal` (links to the web client and the movie).
  A raccoon also gets `raccoon_alert_count` extra "RACCOON ALERT!!!" messages. Failed alerts are retried
  5 times.

### What's in an event

| File | |
|---|---|
| `me_movie.mp4` | real time, from `pre_roll` before the motion to `post_roll` after it |
| `me_rep_image.jpg` | the frame that best shows what matched |
| `me_ollama.json` | `{"legs": false, "person": true, "cat": false, ...}` |
| `me_data.json` | `me_name`, `me_group`, `me_event_group` (the day), `me_time` (movie length), `me_frame_count`, `me_delta_array` (one entry per analysed frame), and `cmo_watch`: the camera type, labels, model, and the Ollama calls' context, tokens and time (`llm`) |

## Settings: `<camera>.properties`

A Java-style properties file: `key = value` lines, `#` comments, and a trailing `\` to continue a line.
Every setting is explained in the files themselves and has a default in `cmo_watch.py` (`DEFAULTS`), so a
line can be commented out to use the default. Values are converted to the type of their default; an
unknown key stops cmo_watch with an error. Paths are relative to the file and may start with `~`.
`me_group` is required.

| Key | |
|---|---|
| `camera.type` | `v4l2` or `rtsp` |
| `camera.device`, `.bitrate`, `.keyframe_seconds` | v4l2: the device, and the camera's H.264 encoder settings |
| `camera.url`, `.credentials_file`, `.audio` | rtsp: the URL **without** the login, the file holding it, and whether to record the audio |
| `camera.width`, `.height`, `.fps` | the video's size (v4l2: asked of the camera; rtsp: the stream's size) |
| `upload.servers` | comma separated URLs; empty = the shared `cmo_server` |
| `motion.masks` | `x0,y0,x1,y1` boxes (0-1 of the picture) to ignore, separated by `;` |
| `camera.input_options` | extra ffmpeg options, as on a command line |
| `camera.source` | a movie file to play instead of the camera (testing) |

**The camera password** is never in a properties file, git, or a command line. `camera.credentials_file`
(`~/.config/porch_watch.env`, mode 600) holds `CAM_USER=...` and `CAM_PASS=...` lines; without one, the
`CAM_USER`/`CAM_PASS` environment variables are used. ffmpeg gets the camera URL with the login in a
private (mode 600) list file in `$XDG_RUNTIME_DIR`, deleted when ffmpeg stops, and a login in an ffmpeg
error message is blanked out before it is logged.

**A new camera**: copy the properties file of the camera most like it, change `me_group` and the
`camera.*` settings, configure telegram-send for its bot on that machine, and add a service (below).

## Running

```sh
python3 cmo_watch.py pet_door.properties                    # on the Pi
.venv/bin/python cmo_watch.py porch_cam.properties          # on mckada
.venv/bin/python cmo_watch.py porch_cam.properties --no-upload -v                      # keep events, more logging
.venv/bin/python cmo_watch.py porch_cam.properties --source clip.mp4 --exit-on-eof --no-upload   # a test clip
.venv/bin/python cmo_upload.py porch_cam.properties         # send anything still pending, now
tail -f ~/logs/porch_watch.log ~/logs/call_ollama.log
```

`--source` plays the clip in real time instead of the camera. Turn `google_home.enabled` off in a copy of
the settings for tests, or a cat in the clip will be announced on the speakers.

Each camera keeps its buffer, its events and its rejects in `local/<me_group>/` (not in git). Logs are
rolled over at 5 MB; `CMO_LOG_DIR` moves them and cmo_ollama's `call_ollama.log`.

### Setup

- **Pi** (Raspberry Pi OS 10, Python 3.7): `python3-numpy`, `python3-pil`, `python3-requests`, `ffmpeg`
  and `v4l2-ctl` from the OS, no venv.
- **mckada**: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, and `ffmpeg`.
- Both: an up-to-date `~/dev/cmo_ollama` (with `jq` and `curl`).

### Telegram

Each machine's `~/.config/telegram-send.conf` (`telegram-send --configure`, then `chmod 600`) holds its
camera's bot token; the token never goes in a properties file or git. `telegram.config` can point at
another file, e.g. for two cameras on one machine with different bots. On the Pi, telegram-send 0.34 is
in its own venv, as the newest version that runs on Python 3.7:

```sh
python3 -m venv ~/.local/telegram-send-venv
~/.local/telegram-send-venv/bin/pip install --upgrade "pip<24.1"
~/.local/telegram-send-venv/bin/pip install "telegram-send==0.34" "python-telegram-bot==13.15"
ln -s ~/.local/telegram-send-venv/bin/telegram-send ~/.local/bin/telegram-send
```

On mckada it is `pipx install telegram-send`.

### `broadcast.py`: say something on the Google Home speakers

```
./broadcast.py cat                          # the message set for "cat" (google_home.message.cat)
./broadcast.py raccoon
./broadcast.py "Dinner is ready"            # any text (at most 200 characters)
./broadcast.py -d bedroom -d upstairs "Hi"  # only these speakers (names from google_home.devices, or addresses)
./broadcast.py --list                       # the speakers, and whether each one answers (no sound)
./broadcast.py -n raccoon                   # dry run: what would be said where, without a sound
```

`-t` says a label word as written, `--language` and `--timeout` change the voice and the wait, and
`--config` reads the speakers from another camera's settings (default `pet_door.properties`). A speaker
that doesn't answer is shown as `FAILED` and the exit code is 1. It uses only Python's standard library:
the speech is an MP3 from Google Translate's text-to-speech, played over the Cast protocol.

### As a service (`systemd/`)

```sh
# Pi: a system service
sudo cp systemd/cmo_watch_pet_door.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cmo_watch_pet_door
sudo systemctl restart cmo_watch_pet_door           # after a git pull or a settings change

# mckada: a user service (loginctl enable-linger, so it starts at boot)
cp systemd/cmo_watch_porch_cam.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now cmo_watch_porch_cam
systemctl --user restart cmo_watch_porch_cam
```

Stopping (SIGTERM) finishes the open segment and classifies and uploads the events already detected.
Crashes show in `journalctl -u cmo_watch_pet_door` / `journalctl --user -u cmo_watch_porch_cam`.

## Left out of the porch camera's old code

Kept in the `porch_cam` repo's history, not needed here:

- recording the camera's 2560x1440 HEVC main stream (it stalls on this camera; the sub stream is
  recorded), with the sub stream as a backup when the main movie had gaps;
- triggering on the camera's own ONVIF motion events (tested 2026-10-09: the camera sends a fixed
  heartbeat, not real detections), and `jarnex.py` / `onvif_events.py`, its ONVIF tools;
- Telegram without a cmo_server (sending the movie itself).
