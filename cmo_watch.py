#!/usr/bin/env python3
"""Watch a camera, save movies that show a person, legs, cat, dog, raccoon or animal, upload them to
cmo_server, and send alerts (Telegram, Google Home). One program for every CMO camera; each camera has its
own settings file (pet_door.properties, porch_cam.properties, see README.md):

  python3 cmo_watch.py pet_door.properties
  .venv/bin/python cmo_watch.py porch_cam.properties

camera.type says how the video arrives:
  v4l2   a camera with its own H.264 encoder on this machine (the pet door Pi's /dev/video0)
  rtsp   a network camera's RTSP stream (the porch camera). The login comes from camera.credentials_file
         and never goes on a command line: ffmpeg is given the URL in a private (mode 600) list file.

Pipeline (one ffmpeg process, three threads, one cmo_ollama check per motion event):

  camera     ffmpeg reads the camera's H.264 stream and
               a) stream-copies it into a ring buffer of 4 s MP4 segments (almost no CPU), and
               b) decodes it to small RGB frames at motion.fps (4) for the detector.
             Segments older than buffer.keep_minutes are deleted unless an event still needs them. If no
             new frame or segment arrives for buffer.stall_seconds, ffmpeg is restarted.
  detector   numpy motion score per frame. Motion that lasts min_motion_frames opens an event, which
             closes after post_roll seconds of quiet (or at max_event seconds). While it runs, the
             max_frames sharpest, time-spread moving frames are kept (O(k) memory, not the whole event).
  worker     once the buffer covers [start - pre_roll, end + post_roll], the segments are joined
             (stream copy) into the event's me_movie.mp4, and the shared cmo_ollama script
             call_ollama_movie.bash is asked about it (classifier.cmo_ollama_dir, ~/dev/cmo_ollama). On a
             match the Google Home speakers are told at once (google_home.*), and the event folder is
             written to events_dir; events with nothing in them are deleted (unless save_rejects).
  uploader   sends each event folder to every cmo_server in upload.servers (see cmo_upload.py), then the
             Telegram alert (telegram.*), and keeps the folder on disk until all of that is done.

Test without the camera by playing a file in real time:
  python3 cmo_watch.py pet_door.properties --source some_movie.mp4 --exit-on-eof --no-upload
"""
import argparse
import contextlib
import copy
import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import version
from cmo_upload import CmoUploader
from vision import FramePicker, MotionScorer, frame_rank, make_grid, to_jpeg

log = logging.getLogger("cmo")

HERE = Path(__file__).resolve().parent
call_ollama = None      # the shared classifier, imported from classifier.cmo_ollama_dir by use_cmo_ollama()
SEG_RE = re.compile(r"^seg_(\d{8}-\d{6})\.mp4$")
STOP = threading.Event()

DEFAULTS = {
    "me_group": "",                      # the camera's name on cmo_server (pet_door, porch_cam); required
    "camera": {
        "type": "v4l2",                  # v4l2 (a local camera with an H.264 encoder) or rtsp (a network camera)
        # v4l2
        "device": "/dev/video0",
        "bitrate": 2000000,              # bits/s of the camera's H.264 encoder
        "keyframe_seconds": 1,           # where a movie can start; also how finely segments are cut
        # rtsp: the URL without the login, which comes from credentials_file (CAM_USER=..., CAM_PASS=...
        # lines, the file mode 600) or the CAM_USER and CAM_PASS environment variables
        "url": "",
        "credentials_file": "",
        "audio": False,                  # also record the camera's audio (converted to AAC)
        # both: the size of the camera's video (v4l2: the size asked for; rtsp: the stream's size, used for
        # the motion check's frames) and, for v4l2, its frame rate
        "width": 640, "height": 480, "fps": 30,
        "input_options": [],             # extra ffmpeg options before -i, e.g. ["-c:v", "h264_mmal"]
        "source": None,                  # a movie file instead of the camera (for testing)
    },
    # dir: empty = local/<me_group>/buffer next to this program
    "buffer": {"dir": "", "segment_seconds": 4, "keep_minutes": 5, "stall_seconds": 20},
    "motion": {
        "fps": 4, "analysis_width": 640,
        "pixel_threshold": 14,           # gray-level change (0-255) that counts a 4x4 block as changed
        "motion_threshold": 0.01,        # fraction of the frame that must change to count as motion
        "min_motion_frames": 2,          # moving frames (within twice that many) that open an event
        "min_event_frames": 3,           # events with fewer moving frames are dropped without asking the LLM
        "masks": [],                     # [x0, y0, x1, y1] boxes (0-1) to ignore, e.g. a clock overlay
    },
    "event": {"pre_roll": 3.0, "post_roll": 5.0, "max_event": 120.0},
    "classifier": {
        # the shared classifier: each event's movie goes to its call_ollama_movie.bash, the same script and
        # question every camera uses and cmo_ollama's labeled clips are tested with. The Ollama
        # server and the question are set in its ollama_common.bash, shared by every machine.
        "cmo_ollama_dir": "~/dev/cmo_ollama",
        "model": "qwen3-vl:8b",
        "question": "person_animal_question",
        # extra call_ollama_movie.bash options for this camera, as on a command line, e.g. "-s motion -c"
        # (see that script); empty = the defaults in ollama_common.bash, the same for every camera
        "movie_options": [],
        # best moving frames kept during an event (at least min_gap seconds apart), to choose
        # me_rep_image.jpg from
        "max_frames": 6, "min_gap": 2.0,
        "rep_frame_llm": True,           # ask cmo_ollama which of them shows the match best (usually one call)
        "upload_unclassified": True,     # cmo_ollama can't answer: upload the event anyway (all labels false)
    },
    "events_dir": "",                    # empty = local/<me_group>/events next to this program
    "upload": {
        # empty = the cmo_server set in cmo_ollama's ollama_common.bash, so every camera uses the same one
        "servers": [],
        "retry_seconds": 60,
        "delete_after_upload": True,
        "keep_days": 3,
    },
    "telegram": {
        # send each uploaded event to Telegram with telegram-send (see telegram_notify.py)
        "enabled": False,
        "program": "",                   # empty = telegram-send on the PATH, else ~/.local/bin/telegram-send
        "config": "",                    # empty = ~/.config/telegram-send.conf
        "raccoon_alert_count": 10,       # extra "RACCOON ALERT!!!" messages when a raccoon is seen
    },
    "google_home": {
        # say a message on Google Home speakers when an event shows one of these labels (google_home.py)
        "enabled": False,
        "devices": "",                   # name=address, comma separated
        "language": "en",
        # message for each label; empty = no announcement. Several labels: their messages in this order.
        "message": {"raccoon": "", "cat": "", "dog": "", "person": "", "legs": "", "animal": ""},
        # a label is said again only after quiet_minutes with no event showing it; 0 = every event
        "quiet_minutes": 0.0,
        # said instead, every remind_minutes, while a label keeps showing up; empty = no reminder
        "reminder": {"raccoon": "", "cat": "", "dog": "", "person": "", "legs": "", "animal": ""},
        "remind_minutes": 15.0,
    },
    "health": {
        # a Telegram message when Ollama, the camera, a cmo_server or a disk is down, and when it is fixed
        # (see health.py)
        "enabled": True,
        "check_seconds": 60,
        "down_minutes": 5.0,             # how long a problem must last before the message
        "repeat_hours": 24.0,            # a reminder while it stays down; 0 = never
        "min_free_gb": 2.0,              # less free disk than this (here, or on a cmo_server) is a problem
    },
    "report": {
        # a daily Telegram message with what was seen and the health (see daily.py)
        "enabled": True,
        "time": "09:00",                 # local time, HH:MM
        "history_days": 31,              # how long local/<me_group>/history.jsonl keeps what happened
    },
    "save_rejects": False,               # keep a grid jpg + json of events with no target, for tuning
    "log_file": "",                     # empty = ~/logs/<me_group>.log (call_ollama.LOG_DIR, or $CMO_LOG_DIR)
}
CAMERA_TYPES = ("v4l2", "rtsp")


# ---- config file ----------------------------------------------------------
def read_properties(path):
    """A Java-style .properties file as {key: value string}: "key = value" or "key: value" lines, # or !
    comments, and a trailing backslash to continue a line."""
    props, pending = {}, ""
    for raw in Path(path).read_text().splitlines():
        line = pending + raw.strip()
        pending = ""
        if not line or line[0] in "#!":
            continue
        if line.endswith("\\") and not line.endswith("\\\\"):
            pending = line[:-1]
            continue
        m = re.match(r"([^=:\s]+)\s*[=:]?\s*(.*)$", line)
        props[m.group(1)] = m.group(2).strip()
    if pending:
        props.update(read_properties_line(pending))
    return props


def read_properties_line(line):
    m = re.match(r"([^=:\s]+)\s*[=:]?\s*(.*)$", line.strip())
    return {m.group(1): m.group(2).strip()} if m else {}


def split_list(v):
    return [x.strip() for x in v.split(",") if x.strip()]


def parse_masks(v):
    """'0,0,0.3,0.1; 0.8,0.9,1,1' -> [[0.0, 0.0, 0.3, 0.1], [0.8, 0.9, 1.0, 1.0]]"""
    boxes = []
    for box in v.split(";"):
        if box.strip():
            nums = [float(x) for x in split_list(box)]
            if len(nums) != 4:
                raise ValueError("a mask is x0,y0,x1,y1, got: " + box.strip())
            boxes.append(nums)
    return boxes


def parse_bool(v):
    if v.lower() in ("true", "yes", "on", "1"):
        return True
    if v.lower() in ("false", "no", "off", "0"):
        return False
    raise ValueError("expected true or false, got: " + v)


# Values that are not a plain number, true/false or text
PARSERS = {
    "camera.source": lambda v: v or None,                  # empty = the camera
    "camera.input_options": shlex.split,                   # as on a command line
    "classifier.movie_options": shlex.split,               # as on a command line
    "motion.masks": parse_masks,
    "upload.servers": lambda v: [{"url": u, "active": True} for u in split_list(v)],
}


def flatten(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            for item in flatten(v, prefix + k + "."):
                yield item
        else:
            yield prefix + k, v


def load_config(path):
    """DEFAULTS, overridden by the keys set in the properties file (camera.width = 1296, ...). Each value
    is converted to the type of its default; an unknown key is an error, so a typo doesn't go unnoticed."""
    path = Path(path)
    conf = copy.deepcopy(DEFAULTS)
    defaults = dict(flatten(DEFAULTS))
    for key, text in (read_properties(path) if path.exists() else {}).items():
        if key not in defaults:
            raise SystemExit("error: {}: unknown setting {!r}".format(path, key))
        default = defaults[key]
        try:
            if key in PARSERS:
                value = PARSERS[key](text)
            elif isinstance(default, bool):
                value = parse_bool(text)
            elif isinstance(default, int):
                value = int(text)
            elif isinstance(default, float):
                value = float(text)
            else:
                value = text
        except ValueError as e:
            raise SystemExit("error: {}: {} = {}: {}".format(path, key, text, e))
        node = conf
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    if not conf["me_group"]:
        raise SystemExit("error: {}: me_group is not set (the camera's name on cmo_server)".format(path))
    if conf["camera"]["type"] not in CAMERA_TYPES:
        raise SystemExit("error: {}: camera.type = {}: expected {}".format(path, conf["camera"]["type"],
                                                                        " or ".join(CAMERA_TYPES)))
    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", conf["report"]["time"]):
        raise SystemExit("error: {}: report.time = {}: expected HH:MM, e.g. 09:00".format(path, conf["report"]["time"]))
    if conf["camera"]["type"] == "rtsp" and not conf["camera"]["url"] and not conf["camera"]["source"]:
        raise SystemExit("error: {}: camera.type = rtsp needs camera.url".format(path))
    base = path.resolve().parent
    local = HERE / "local" / conf["me_group"]
    conf["classifier"]["cmo_ollama_dir"] = str(Path(os.path.expanduser(conf["classifier"]["cmo_ollama_dir"])))
    conf["events_dir"] = str(base / os.path.expanduser(conf["events_dir"]) if conf["events_dir"] else local / "events")
    conf["buffer"]["dir"] = str(base / os.path.expanduser(conf["buffer"]["dir"]) if conf["buffer"]["dir"]
                                else local / "buffer")
    if conf["log_file"]:
        conf["log_file"] = str(base / os.path.expanduser(conf["log_file"]))
    if conf["camera"]["credentials_file"]:
        conf["camera"]["credentials_file"] = os.path.expanduser(conf["camera"]["credentials_file"])
    return conf


# ---- camera login (rtsp) --------------------------------------------------
def read_credentials(path):
    """(user, password) from CAM_USER=... and CAM_PASS=... lines (the systemd EnvironmentFile format), else
    from the CAM_USER and CAM_PASS environment variables. The file should be mode 600."""
    found = {}
    if path:
        for line in Path(path).read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?(CAM_USER|CAM_PASS)\s*=\s*(.*?)\s*$", line)
            if m:
                value = m.group(2)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                found[m.group(1)] = value
    user = found.get("CAM_USER") or os.environ.get("CAM_USER")
    password = found.get("CAM_PASS") or os.environ.get("CAM_PASS")
    return user, password


def with_login(url, user, password):
    """rtsp://host/path -> rtsp://user:password@host/path (no login: the URL unchanged)."""
    if not (user and password):
        return url
    from urllib.parse import quote
    return re.sub(r"^(\w+://)", lambda m: "{}{}:{}@".format(m.group(1), quote(user, safe=""),
                                                             quote(password, safe="")), url, count=1)


def redact(text):
    """Hide a login in text that may hold a camera URL (ffmpeg's error messages can)."""
    return re.sub(r"//[^/@\s]+@", "//***@", str(text))


@contextlib.contextmanager
def ffmpeg_input(url, **options):
    """ffmpeg arguments that open an RTSP url. The URL carries the camera's password, and a command line
    is visible to every local user (ps), so the URL and its RTSP options go in a private (mode 600)
    ffconcat list file instead, and ffmpeg is pointed at that. The file is deleted when the block ends, so
    run ffmpeg inside it."""
    options = dict({"rtsp_transport": "tcp", "timeout": 10000000}, **options)   # timeout in microseconds
    # mkstemp creates the file mode 600; XDG_RUNTIME_DIR is a per-user tmpfs (mode 700) when there is one
    fd, path = tempfile.mkstemp(prefix="cmo_watch-", suffix=".ffconcat", dir=os.environ.get("XDG_RUNTIME_DIR"))
    try:
        with os.fdopen(fd, "w") as f:
            f.write("ffconcat version 1.0\nfile '{}'\n".format(url))
            f.writelines("option {} {}\n".format(k, v) for k, v in options.items())
        yield ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file,rtsp,rtp,tcp,udp", "-i", path]
    finally:
        os.unlink(path)


# ---- ffmpeg helpers -------------------------------------------------------
def probe_size(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    s = json.loads(r.stdout.decode())["streams"][0]
    return int(s["width"]), int(s["height"])


def probe_movie(path):
    """(duration_s, video_frames) of a movie file."""
    r = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_packets:format=duration", "-of", "json", str(path)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    j = json.loads(r.stdout.decode())
    return float(j["format"]["duration"]), int(j["streams"][0]["nb_read_packets"])


def probe_codec(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (r.stdout.decode().split() or [""])[0]


def analysis_size(w, h, max_width):
    if w > max_width:
        w, h = max_width, round(h * max_width / w)
    return w - w % 4, h - h % 4


# ---- camera + ring buffer -------------------------------------------------
class Camera(threading.Thread):
    """One ffmpeg process: the camera's video stream-copied into the segment ring buffer, and the same
    stream decoded to small RGB frames for the detector (a v4l2 camera can only be opened once, and a
    network camera needs only one connection)."""

    def __init__(self, conf, detector, hold, login=(None, None)):
        super().__init__(name="camera", daemon=True)
        self.c, self.b, self.m = conf["camera"], conf["buffer"], conf["motion"]
        self.detector = detector
        self.hold = hold            # callable -> oldest wall time still needed by a pending event, or None
        self.dir = Path(self.b["dir"])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.source = self.c["source"]
        self.type = self.c["type"]
        self.url = with_login(self.c["url"], *login) if self.type == "rtsp" else None
        if self.source:
            w, h = probe_size(self.source)
        else:
            w, h = self.c["width"], self.c["height"]
        self.size = analysis_size(w, h, self.m["analysis_width"])
        self.proc = None
        self.last_frame = time.monotonic()
        self.eof = threading.Event()      # file source ended (or the camera thread stopped)
        self.stats = {"restarts": 0, "stalls": 0}
        self.history = None         # daily.History, for the daily report
        self.stalled = False

    def name(self):
        """What is being watched, for the log (never with the login)."""
        return self.source or (self.c["url"] if self.type == "rtsp" else self.c["device"])

    def setup_camera(self):
        """The camera's H.264 encoder: a keyframe every keyframe_seconds, and SPS/PPS repeated before each
        one so every segment can be decoded on its own."""
        c = self.c
        ctrls = "repeat_sequence_header=1,h264_i_frame_period={},video_bitrate={}".format(
            int(c["fps"] * c["keyframe_seconds"]), int(c["bitrate"]))
        r = subprocess.run(["v4l2-ctl", "-d", c["device"], "-c", ctrls], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        if r.returncode != 0:
            log.warning("camera: v4l2-ctl -c %s failed: %s", ctrls, r.stderr.decode(errors="replace").strip())

    def command(self, rtsp_input=()):
        """The ffmpeg command. rtsp_input: the arguments from ffmpeg_input() that open the camera."""
        c, b = self.c, self.b
        w, h = self.size
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + list(c["input_options"])
        audio = []
        if self.source:
            cmd += ["-re", "-i", str(self.source)]
        elif self.type == "rtsp":
            cmd += list(rtsp_input)
            if c["audio"]:
                # MP4 can't hold the G.711 audio such cameras send: convert just the audio to AAC
                audio = ["-map", "0:a?", "-c:a", "aac", "-b:a", "32k"]
        else:
            cmd += ["-f", "v4l2", "-input_format", "h264", "-framerate", str(c["fps"]),
                    "-video_size", "{}x{}".format(c["width"], c["height"]), "-i", c["device"]]
        # a) the video as the camera made it, cut into MP4 segments named by their wall-clock start
        cmd += ["-map", "0:v:0"] + audio + ["-c:v", "copy", "-f", "segment",
                                            "-segment_time", str(b["segment_seconds"]),
                                            "-segment_format", "mp4", "-reset_timestamps", "1", "-strftime", "1",
                                            str(self.dir / "seg_%Y%m%d-%H%M%S.mp4")]
        # b) small RGB frames for the motion check
        cmd += ["-map", "0:v:0", "-an", "-vf", "fps={},scale={}:{}".format(self.m["fps"], w, h),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        return cmd

    def run(self):
        backoff = 2
        while not STOP.is_set():
            if not self.source and self.type == "v4l2":
                self.setup_camera()
            log.info("camera: starting on %s", self.name())
            t0 = time.monotonic()
            # stderr to a file, not a pipe: a long run could fill the pipe and block ffmpeg
            errf = tempfile.TemporaryFile()
            with (ffmpeg_input(self.url) if self.url and not self.source else contextlib.nullcontext(())) as inp:
                self.proc = subprocess.Popen(self.command(inp), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=errf, bufsize=0)
                self.last_frame = time.monotonic()
                watchdog = threading.Thread(target=self.watch, args=(self.proc,), name="watchdog", daemon=True)
                watchdog.start()
                self.read_frames(self.proc)
                self.stop_ffmpeg()
            self.detector.stream_ended()
            if STOP.is_set():
                break
            if self.source:
                log.info("camera: %s ended", self.source)
                self.eof.set()
                break
            self.stats["restarts"] += 1
            if self.history:
                self.history.record("camera_restart", stall=self.stalled)
            self.stalled = False
            errf.seek(0)
            # ffmpeg's error text can hold the camera URL with its password
            err = redact(errf.read().decode(errors="replace").strip()[-300:])
            log.warning("camera: ffmpeg exited (%s) %s", self.proc.returncode, err)
            backoff = 2 if time.monotonic() - t0 > 60 else min(backoff * 2, 60)
            STOP.wait(backoff)
        self.eof.set()

    def read_frames(self, proc):
        w, h = self.size
        n = w * h * 3
        while not STOP.is_set():
            buf = b""
            while len(buf) < n:
                chunk = proc.stdout.read(n - len(buf))
                if not chunk:
                    return
                buf += chunk
            self.last_frame = time.monotonic()
            self.detector.frame(time.time(), np.frombuffer(buf, np.uint8).reshape(h, w, 3))

    def watch(self, proc):
        """Prune the ring buffer, and kill ffmpeg if the camera stops sending video."""
        newest, newest_at = None, time.monotonic()
        stall = self.b["stall_seconds"]
        while proc.poll() is None and not STOP.is_set():
            self.prune()
            segs = self.segments()
            if segs and segs[-1][1] != newest:
                newest, newest_at = segs[-1][1], time.monotonic()
            quiet = max(time.monotonic() - newest_at, time.monotonic() - self.last_frame)
            if stall and not self.source and quiet > stall:
                self.stats["stalls"] += 1
                self.stalled = True
                log.warning("camera: STALL, no new video for %.0fs, restarting ffmpeg", quiet)
                proc.kill()
                return
            STOP.wait(2)

    def stop_ffmpeg(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()          # lets ffmpeg finish the current segment's MP4 header
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def segments(self):
        """[(start_wall_time, path)] sorted by start."""
        out = []
        for p in self.dir.iterdir():
            m = SEG_RE.match(p.name)
            if m:
                out.append((datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp(), p))
        return sorted(out)

    def prune(self):
        cutoff = time.time() - self.b["keep_minutes"] * 60
        held = self.hold()
        if held is not None:
            cutoff = min(cutoff, held)
        segs = self.segments()
        for (s, p), (nxt, _) in zip(segs, segs[1:]):   # never touch the newest (being written)
            if nxt < cutoff:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    def covered_until(self):
        """Wall time up to which the buffer is complete (start of the segment being written)."""
        segs = self.segments()
        return segs[-1][0] if segs else 0.0

    def clip(self, start, end, dest):
        """Join the segments overlapping [start, end] into dest (stream copy). Returns True on success."""
        segs = self.segments()
        pick = [(s, p) for i, (s, p) in enumerate(segs)
                if s < end and (segs[i + 1][0] if i + 1 < len(segs) else float("inf")) > start]
        if not pick:
            log.error("clip: no buffered video between %s and %s", datetime.fromtimestamp(start),
                      datetime.fromtimestamp(end))
            return False
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            for i, (s, p) in enumerate(pick):
                f.write("file '{}'\n".format(p.resolve()))
                if i == 0 and start > s:
                    f.write("inpoint {:.2f}\n".format(start - s))
                if i == len(pick) - 1 and end - s < 3600:
                    f.write("outpoint {:.2f}\n".format(max(end - s, 0.5)))
            listfile = f.name
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat",
               "-safe", "0", "-i", listfile, "-c", "copy", "-movflags", "+faststart"]
        if probe_codec(pick[0][1]) == "hevc":
            cmd += ["-tag:v", "hvc1"]           # lets QuickTime and Apple devices play an HEVC movie
        r = subprocess.run(cmd + [str(dest)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.unlink(listfile)
        if r.returncode != 0:
            log.error("clip ffmpeg failed: %s", r.stderr.decode(errors="replace").strip()[-300:])
        return r.returncode == 0


# ---- detector -------------------------------------------------------------
class Event:
    def __init__(self, start, picker):
        self.start = start
        self.end = 0.0
        self.frames = 0                 # moving frames
        self.peak_motion = 0.0
        self.picker = picker
        self.deltas = []                # (wall time, motion score, moving) for every analysed frame


class Detector:
    """Called from the camera thread with each analysed frame."""

    def __init__(self, conf, size, events):
        self.m, self.e, self.cl = conf["motion"], conf["event"], conf["classifier"]
        self.size = size
        self.events = events
        self.reset_stream()

    def reset_stream(self):
        self.scorer = MotionScorer(*self.size, masks=[tuple(x) for x in self.m["masks"]],
                                   pixel_threshold=self.m["pixel_threshold"])
        self.ev, self.recent, self.quiet_since = None, [], None

    def frame(self, now, rgb):
        m, e = self.m, self.e
        score = self.scorer.score(rgb)
        moving = score >= m["motion_threshold"]
        self.recent = (self.recent + [moving])[-max(m["min_motion_frames"] * 2, 2):]
        ev = self.ev
        if ev is None:
            if sum(self.recent) < m["min_motion_frames"]:
                return
            ev = self.ev = Event(now - len(self.recent) / m["fps"],
                                 FramePicker(k=self.cl["max_frames"], min_gap=self.cl["min_gap"]))
            log.info("event: motion started (%.3f)", score)
        ev.deltas.append((now, score, moving))
        if moving:
            ev.frames += 1
            ev.peak_motion = max(ev.peak_motion, score)
            ev.picker.add(frame_rank(score, rgb), now, rgb)
            self.quiet_since = None
        elif self.quiet_since is None:
            self.quiet_since = now
        if (self.quiet_since and now - self.quiet_since >= e["post_roll"]) or now - ev.start >= e["max_event"]:
            ev.end = self.quiet_since or now
            self.events.put(ev)
            self.ev, self.recent, self.quiet_since = None, [], None

    def stream_ended(self):
        """ffmpeg stopped: still classify an event that was running, then start fresh."""
        if self.ev is not None:
            self.ev.end = time.time()
            self.events.put(self.ev)
        self.reset_stream()


# ---- classifier + event writer ---------------------------------------------
def upload_servers(conf):
    """upload.servers, or (when empty) the cmo_server in cmo_ollama's ollama_common.bash, shared by every
    camera, so in any setup (production, or a test server) they all go to the same server."""
    servers = conf["upload"]["servers"]
    if servers:
        return servers
    url = call_ollama.setting("cmo_server")
    if not url:
        raise SystemExit("error: upload.servers is empty and cmo_ollama's ollama_common.bash has no cmo_server")
    return [{"url": url, "active": True}]


def google_home_announcer(conf):
    """The Google Home announcer for the worker when google_home.enabled, else None."""
    gh = conf["google_home"]
    if not gh["enabled"]:
        return None
    from google_home import Announcer, parse_devices
    devices = parse_devices(gh["devices"])
    if not devices:
        raise SystemExit("error: google_home.enabled but google_home.devices is empty")
    announcer = Announcer(devices, gh["message"], gh["language"], quiet_minutes=gh["quiet_minutes"],
                          reminders=gh["reminder"], remind_minutes=gh["remind_minutes"])
    log.info("google home: %s on %s", ", ".join("{} -> {!r}".format(k, v) for k, v in announcer.messages.items())
             or "no messages set", ", ".join(devices))
    if announcer.quiet:
        log.info("google home: quiet for %.0f min after each label%s", gh["quiet_minutes"],
                 "".join("; {} reminder every {:.0f} min: {!r}".format(k, gh["remind_minutes"], v)
                         for k, v in announcer.reminders.items()) if announcer.remind else "")
    return announcer


def telegram_notifier(conf):
    """The Telegram notifier for the uploader when telegram.enabled, else None."""
    tg = conf["telegram"]
    if not tg["enabled"]:
        return None
    from telegram_notify import TelegramNotifier
    notifier = TelegramNotifier(conf["me_group"], tg["program"] or None, tg["config"] or None,
                                tg["raccoon_alert_count"])
    err = notifier.check()
    if err:
        raise SystemExit("error: " + err)
    log.info("telegram alerts with %s", notifier.program)
    return notifier


def health_monitor(conf, camera, worker, servers, notifier, history=None):
    """The health monitor (health.py) when health.enabled, else None. Without a notifier it only logs."""
    h = conf["health"]
    if not h["enabled"]:
        return None
    import health
    checks = [health.ollama_check(call_ollama.setting("api"), worker)]
    if not conf["camera"]["source"]:
        checks.append(health.camera_check(camera, h["check_seconds"]))
    for url in servers:
        checks += health.server_checks(url.rstrip("/") + "/", h["min_free_gb"])
    checks.append(health.disk_check(conf["events_dir"], h["min_free_gb"]))
    log.info("health: checking %s every %ss; a problem is reported after %s min%s", ", ".join(c.what for c in checks),
             h["check_seconds"], h["down_minutes"], " on Telegram" if notifier else " (log only, no Telegram)")
    return health.HealthMonitor(conf["me_group"], checks, notifier, h["check_seconds"], h["down_minutes"],
                                h["repeat_hours"], stop=STOP, history=history)


def server_free_bytes(servers):
    """Free disk on the first cmo_server (its /catcam/health), or None."""
    if not servers:
        return None
    import requests
    try:
        return requests.get(servers[0].rstrip("/") + "/catcam/health", timeout=20).json().get("free_bytes")
    except Exception:
        return None


def daily_reporter(conf, history, health, uploader, servers, notifier, started):
    """The daily report thread (daily.py) when report.enabled, else None. Without a notifier it only logs."""
    if not conf["report"]["enabled"]:
        return None
    import daily
    version = code()

    def make(since, now):
        return daily.build(conf["me_group"], history, since, now, health.status() if health else None,
                           len(uploader.pending()) if uploader else None, conf["events_dir"],
                           server_free_bytes(servers), version, started)
    log.info("daily report at %s%s", conf["report"]["time"], " on Telegram" if notifier else " (log only)")
    return daily.DailyReporter(conf["me_group"], history, conf["report"]["time"], make, notifier, stop=STOP)


def code():
    """The code running, for log lines and the daily report: "cmo_client:1.3.0, cmo_ollama:1.2.0"."""
    return "{}, {}".format(version.CODE, call_ollama.code() if call_ollama else "cmo_ollama:unknown")


def created_by(calls):
    """me_data.json's created_by: {"cmo_client": "1.3.0", "cmo_ollama": "1.2.0"}. cmo_ollama's version is the
    one its scripts reported for this event's calls (they read it on every call, so a git pull there shows up
    without a restart), else what it says now."""
    ollama = next((c["code"] for c in calls if c.get("code")), None) or call_ollama.code()
    return {"cmo_client": version.VERSION, "cmo_ollama": ollama.split(":", 1)[-1]}


def use_cmo_ollama(path):
    """Import the shared classifier (call_ollama.py) from the cmo_ollama folder."""
    global call_ollama
    sys.path.insert(0, str(path))
    import call_ollama as module
    call_ollama = module
    return module


class Worker(threading.Thread):
    """Cuts each event's movie once the buffer covers it, asks cmo_ollama about it, and writes the event
    folder for the uploader only when something was found."""

    def __init__(self, conf, events, camera, uploader, announcer=None, history=None):
        super().__init__(name="worker", daemon=True)
        self.history = history      # daily.History: every event's result, for the daily report
        self.announcer = announcer
        self.conf = conf
        self.e, self.cl = conf["event"], conf["classifier"]
        self.events, self.cam, self.uploader = events, camera, uploader
        self.out = Path(conf["events_dir"])
        self.out.mkdir(parents=True, exist_ok=True)
        self.pending = []           # events waiting for the buffer to cover their end
        self.lock = threading.Lock()
        self.counter = 0
        self.stats = {"events": 0, "matches": 0, "skipped_short": 0, "unclassified": 0, "saved": 0,
                      "llm_seconds": 0.0}
        self.llm_failed_since = None    # wall time of the first failed classification in a row (health.py)

    def oldest_needed(self):
        with self.lock:
            starts = [ev.start - self.e["pre_roll"] for ev in self.pending]
        if not self.events.empty():
            starts.append(time.time() - self.e["max_event"] - self.e["pre_roll"] - 60)
        return min(starts) if starts else None

    def run(self):
        while not (STOP.is_set() and self.cam.eof.is_set() and self.events.empty() and not self.pending):
            try:
                ev = self.events.get(timeout=1)
            except queue.Empty:
                ev = None
            if ev is not None:
                self.handle(ev)
            self.flush_pending()

    def handle(self, ev):
        self.stats["events"] += 1
        if ev.frames < self.conf["motion"]["min_event_frames"]:
            self.stats["skipped_short"] += 1
            self.record(ev, "short")
            log.info("event: %s %.1fs, %d moving frames: too short, ignored",
                     datetime.fromtimestamp(ev.start).strftime("%H:%M:%S"), ev.end - ev.start, ev.frames)
            return
        with self.lock:
            self.pending.append(ev)

    def flush_pending(self):
        """Process the events whose whole time range is on disk (a newer segment has started after it)."""
        done = self.cam.eof.is_set()
        with self.lock:
            ready = [ev for ev in self.pending
                     if done or self.cam.covered_until() > ev.end + self.e["post_roll"]]
        for ev in ready:
            try:
                self.process(ev)
            except Exception:
                log.exception("event failed")
            with self.lock:
                self.pending.remove(ev)

    def process(self, ev):
        self.counter += 1
        start = datetime.fromtimestamp(ev.start)
        dur = ev.end - ev.start
        name = "{:%Y%m%d_%H%M%S}_{:04d}".format(start, self.counter % 10000)
        tmp = self.out / ("." + name)
        shutil.rmtree(str(tmp), ignore_errors=True)
        tmp.mkdir(parents=True)
        t0, t1 = ev.start - self.e["pre_roll"], ev.end + self.e["post_roll"]
        movie = tmp / "me_movie.mp4"
        if not self.cam.clip(t0, t1, movie):
            shutil.rmtree(str(tmp), ignore_errors=True)
            log.error("event %s: no movie, not saved", name)
            return
        movie_calls = []
        labels, secs = call_ollama.classify_movie(movie, self.cl["model"], self.cl["question"],
                                                 options=self.cl["movie_options"], stats=movie_calls)
        self.stats["llm_seconds"] += secs
        # seconds: the whole classification as seen from here; ollama: what the Ollama calls took
        llm = {"seconds": round(secs, 1), "question": self.cl["question"],
               "movie_options": " ".join(self.cl["movie_options"]),
               "ollama": call_ollama.summarize(movie_calls), "ollama_calls": movie_calls}
        took = "{:.1f}s; {}".format(secs, call_ollama.describe(movie_calls))
        if labels is None:
            self.llm_failed_since = self.llm_failed_since or time.time()
        else:
            self.llm_failed_since = None
        cands = ev.picker.best_first()
        if labels is None:
            if not self.cl["upload_unclassified"]:
                log.warning("event: %s %.1fs -> not classified (cmo_ollama failed), dropped (%s)",
                            start.strftime("%H:%M:%S"), dur, took)
                shutil.rmtree(str(tmp), ignore_errors=True)
                self.record(ev, "dropped", seconds=secs)
                return
            self.stats["unclassified"] += 1
            self.record(ev, "unclassified", seconds=secs)
            labels = {k: False for k in call_ollama.LABELS}
            labels["error"] = True
            log.warning("event: %s %.1fs -> not classified (cmo_ollama failed), saving it anyway (%s)",
                        start.strftime("%H:%M:%S"), dur, took)
        hits = [k for k, v in labels.items() if v and k != "error"]
        if not hits and not labels.get("error"):
            log.info("event: %s %.1fs -> no target (%s)", start.strftime("%H:%M:%S"), dur, took)
            self.record(ev, "no_target", seconds=secs, calls=movie_calls)
            shutil.rmtree(str(tmp), ignore_errors=True)
            if self.conf["save_rejects"] and cands:
                self.save_reject(ev, cands, dict(llm, labels=labels))
            return
        self.stats["matches"] += 1
        log.info("event: %s %.1fs -> MATCH %s (%s)", start.strftime("%H:%M:%S"), dur,
                 ",".join(hits) or "(unclassified)", took)
        if not labels.get("error"):
            self.record(ev, "match", seconds=secs, calls=movie_calls, labels=hits)
        if self.announcer:
            self.announcer.announce(hits, name)     # on its own thread, right away (not after the upload)
        best, calls = 0, 0
        if cands and hits and self.cl["rep_frame_llm"]:
            jpgs = []
            for i, c in enumerate(cands):
                jpgs.append(tmp / ".cand_{}.jpg".format(i))
                jpgs[-1].write_bytes(to_jpeg(c.rgb, 90))
            rep_calls = []
            best, calls = call_ollama.pick_representative(jpgs, labels, self.cl["model"], self.cl["question"],
                                                          stats=rep_calls)
            for j in jpgs:
                j.unlink()
            if rep_calls:
                llm["rep_frame_ollama"] = call_ollama.summarize(rep_calls)
                llm["rep_frame_ollama_calls"] = rep_calls
                log.info("event: %s preview picture: frame %d of %d (%s)", start.strftime("%H:%M:%S"), best + 1,
                         len(jpgs), call_ollama.describe(rep_calls))
        llm["rep_frame_calls"] = calls
        rep = cands[best] if cands else None
        self.write_event(ev, name, tmp, t0, labels, llm, rep)

    def record(self, ev, result, seconds=None, calls=(), labels=()):
        """One line in the history for the daily report."""
        if not self.history:
            return
        fields = {"result": result, "start": round(ev.start, 1)}
        if labels:
            fields["labels"] = list(labels)
        if seconds is not None:
            fields["seconds"] = round(seconds, 1)
        tokens = [c.get("prompt_tokens") for c in calls if c.get("prompt_tokens")]
        if tokens:
            fields["prompt_tokens"] = max(tokens)
        self.history.record("event", **fields)

    def write_event(self, ev, name, tmp, t0, labels, llm, rep):
        movie = tmp / "me_movie.mp4"
        duration, frames = probe_movie(movie)
        if rep is not None:
            (tmp / "me_rep_image.jpg").write_bytes(to_jpeg(rep.rgb, 90))
        (tmp / "me_ollama.json").write_text(json.dumps(labels, indent=4))
        movie_start = datetime.fromtimestamp(t0)

        def delta(t, score, moving):
            return {"contours": [], "occupied": bool(moving), "motion": round(score, 4),
                    "event_time_iso": datetime.fromtimestamp(t).isoformat(timespec="milliseconds"),
                    "event_time_ms": round((t - t0) * 1000, 1), "event_start_time": 0}

        me_data = {
            "me_group": self.conf["me_group"], "me_event_group": name[:8], "me_name": name, "me_tag": name,
            "me_time": round(duration, 3), "me_frame_count": frames,
            "me_json_name": "me_data.json", "me_video_name": "me_movie.mp4",
            "me_rep_image": "me_rep_image.jpg" if rep is not None else "",
            "me_image_array": [],
            # the first motion and the end of the event: the web client takes the event's end time from the
            # last entry, and the server's brief listing its start and end. The frames in between went
            # unread and made up most of the file.
            "me_delta_array": [delta(*d) for d in ev.deltas[:1] + ev.deltas[1:][-1:]],
            # the code that made this event and classified it, e.g. {"cmo_client": "1.3.0", "cmo_ollama": "1.2.0"}
            "created_by": created_by((llm or {}).get("ollama_calls") or []),
            "cmo_watch": {"camera": self.conf["camera"]["type"],
                          "movie_start": movie_start.isoformat(timespec="milliseconds"),
                          "motion_frames": ev.frames, "peak_motion": round(ev.peak_motion, 4),
                          "labels": [k for k, v in labels.items() if v and k != "error"],
                          "model": self.cl["model"], "llm": llm},
        }
        (tmp / "me_data.json").write_text(json.dumps(me_data, indent=2))
        final = self.out / name
        tmp.rename(final)
        self.stats["saved"] += 1
        log.info("saved %s (%.1fs, %d frames)", final, duration, frames)
        if self.uploader:
            self.uploader.poke()

    def save_reject(self, ev, cands, llm):
        d = Path(self.conf["events_dir"]).parent / "rejects" / datetime.fromtimestamp(ev.start).strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        stem = datetime.fromtimestamp(ev.start).strftime("%H%M%S")
        (d / (stem + ".jpg")).write_bytes(to_jpeg(make_grid([c.rgb for c in sorted(cands, key=lambda c: c.t)])))
        (d / (stem + ".json")).write_text(json.dumps({"frames": ev.frames, "peak_motion": ev.peak_motion,
                                                      "llm": llm}, indent=2))


# ---- main -----------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="the camera's settings file, e.g. pet_door.properties")
    ap.add_argument("--source", help="play this movie file instead of the camera (testing)")
    ap.add_argument("--exit-on-eof", action="store_true", help="with --source: stop when the file ends")
    ap.add_argument("--no-upload", action="store_true", help="save events but don't upload them")
    ap.add_argument("--report", action="store_true",
                    help="print the daily report for the last 24 hours (not sent) and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    conf = load_config(a.config)
    if a.source:
        conf["camera"]["source"] = str(Path(a.source).resolve())
    cl = conf["classifier"]
    try:
        use_cmo_ollama(cl["cmo_ollama_dir"])
    except ImportError as e:
        sys.exit("error: no call_ollama.py in classifier.cmo_ollama_dir = {}: {}".format(cl["cmo_ollama_dir"], e))
    import daily
    history = daily.History(Path(conf["events_dir"]).parent / "history.jsonl", conf["report"]["history_days"])
    if a.report:
        servers = [] if a.no_upload else [x["url"] for x in upload_servers(conf)]
        print(daily.build(conf["me_group"], history, time.time() - 86400, time.time(), None, None,
                          conf["events_dir"], server_free_bytes(servers), code()))
        return 0
    # the same log folder (and rollover) as every camera and the cmo_ollama scripts
    log_file = conf["log_file"] or str(call_ollama.LOG_DIR / (conf["me_group"] + ".log"))
    call_ollama.setup_log_file(log_file, a.verbose)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("error: {} not found on PATH".format(tool))

    err = call_ollama.check_setup()
    if err:
        sys.exit("error: " + err)
    log.info("classifying with %s (%s, question %s%s)", call_ollama.HERE, cl["model"], cl["question"],
             ", options " + " ".join(cl["movie_options"]) if cl["movie_options"] else "")

    login = (None, None)
    if conf["camera"]["type"] == "rtsp" and not conf["camera"]["source"]:
        try:
            login = read_credentials(conf["camera"]["credentials_file"])
        except OSError as e:
            sys.exit("error: camera.credentials_file: {}".format(e))
        if not all(login):
            log.warning("camera: no login (CAM_USER, CAM_PASS) in camera.credentials_file or the environment")

    uploader = notifier = None
    servers = []
    if not a.no_upload:
        u = conf["upload"]
        servers = upload_servers(conf)
        log.info("uploading to %s", ", ".join(s["url"] for s in servers))
        notifier = telegram_notifier(conf)
        uploader = CmoUploader(conf["events_dir"], conf["me_group"], servers, u["retry_seconds"],
                               u["delete_after_upload"], u["keep_days"], stop=STOP, notifier=notifier)
    events = queue.Queue()
    worker = None
    detector = Detector(conf, (0, 0), events)
    camera = Camera(conf, detector, lambda: worker.oldest_needed(), login)
    detector.size = camera.size
    detector.reset_stream()
    started = time.time()
    history.record("start")
    camera.history = history
    announcer = google_home_announcer(conf)
    if announcer:
        announcer.on_result = lambda ok, failed: history.record("announce", ok=ok, failed=failed)
    worker = Worker(conf, events, camera, uploader, announcer, history)
    server_urls = [x["url"] for x in servers if x.get("active", True)]
    health = health_monitor(conf, camera, worker, server_urls, notifier, history)
    reporter = daily_reporter(conf, history, health, uploader, server_urls, notifier, started)

    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    if reporter:
        # SIGUSR1: send a report for the last 24 hours now (systemctl kill --kill-who=main -s USR1 <service>)
        signal.signal(signal.SIGUSR1, lambda *_: reporter.request())
    for t in [camera, worker] + [x for x in (uploader, health, reporter) if x]:
        t.start()
    log.info("cmo_watch: %s", code())
    log.info("cmo_watch: %s watching %s (%s) at %dx%d, motion at %dx%d %s fps", conf["me_group"], camera.name(),
             conf["camera"]["type"], conf["camera"]["width"], conf["camera"]["height"], camera.size[0],
             camera.size[1], conf["motion"]["fps"])
    last_report = time.monotonic()
    try:
        while not STOP.is_set():
            if camera.eof.wait(1) and a.exit_on_eof:
                break
            if time.monotonic() - last_report > 600:
                last_report = time.monotonic()
                log.info("stats: %s camera=%s upload=%s (%s)", worker.stats, camera.stats,
                         uploader.stats if uploader else None, code())
    except KeyboardInterrupt:
        pass
    log.info("stopping...")
    STOP.set()
    camera.stop_ffmpeg()
    camera.join(10)
    worker.join(120)           # finishes classifying and writing events it already has
    if uploader:
        uploader.poke()        # end its scan loop (STOP is set) before the final pass, so nothing is sent twice
        uploader.join(30)
        uploader.finish(60)    # sends what it can, the rest goes after the next start
    log.info("stats: %s camera=%s upload=%s (%s)", worker.stats, camera.stats, uploader.stats if uploader else None,
             code())
    return 0


if __name__ == "__main__":
    sys.exit(main())
