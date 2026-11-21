"""Speak a message on Google Home / Nest speakers on the local network, for camera alerts.
The command-line tool is broadcast.py; cmo_watch.py uses the Announcer below (google_home.* settings).

A Python version of the Pi's old ~/dev/goolge_home_broadcast (broadcast.js, castv2-client and
google-tts-api). Like those, it turns the text into an MP3 URL of Google Translate's text-to-speech and
tells each speaker, over the Cast protocol (TLS on port 8009), to play that URL in the Default Media
Receiver app. The speaker fetches the audio itself. It uses only the Python standard library (3.7+), so
it runs on the Pi without npm or extra packages. Any camera can use it: set google_home.enabled in
its properties file.

Cast protocol, in short: each message is a 4-byte big-endian length and a protobuf CastMessage
(protocol_version, source_id, destination_id, namespace, payload_type, payload_utf8), the payload being
JSON. Connect to "receiver-0", LAUNCH the Default Media Receiver (CC1AD845), connect to the transportId
it reports, and send it a LOAD with the URL. The speaker keeps playing after we hang up.
"""
import json
import logging
import socket
import ssl
import struct
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

log = logging.getLogger("cmo")

CAST_PORT = 8009
DEFAULT_MEDIA_RECEIVER = "CC1AD845"
NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
TTS_MAX_CHARS = 200          # Google Translate's text-to-speech limit per request


def tts_url(text, language="en"):
    """The MP3 URL of Google Translate speaking text (what google-tts-api's getAudioUrl builds)."""
    text = text.strip()
    if not text:
        raise ValueError("nothing to say")
    if len(text) > TTS_MAX_CHARS:
        raise ValueError("message is {} characters, Google's text-to-speech takes at most {}".format(
            len(text), TTS_MAX_CHARS))
    return "https://translate.google.com/translate_tts?" + urlencode({
        "ie": "UTF-8", "q": text, "tl": language, "total": 1, "idx": 0, "textlen": len(text),
        "client": "tw-ob", "prev": "input", "ttsspeed": 1})


# ---- CastMessage protobuf, by hand (six fields) --------------------------------
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field_string(number, text):
    data = text.encode("utf-8")
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def encode_message(source, destination, namespace, payload):
    """A CastMessage with a JSON payload, framed with its length."""
    body = (_varint(1 << 3) + _varint(0)                 # protocol_version = CASTV2_1_0
            + _field_string(2, source) + _field_string(3, destination) + _field_string(4, namespace)
            + _varint(5 << 3) + _varint(0)               # payload_type = STRING
            + _field_string(6, json.dumps(payload)))
    return struct.pack(">I", len(body)) + body


def decode_message(body):
    """{'source': ..., 'destination': ..., 'namespace': ..., 'payload': dict or None} from a CastMessage."""
    names = {2: "source", 3: "destination", 4: "namespace", 6: "payload"}
    msg, i = {}, 0
    while i < len(body):
        key, i = _read_varint(body, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            _, i = _read_varint(body, i)
        elif wire == 2:
            size, i = _read_varint(body, i)
            if number in names:
                msg[names[number]] = body[i:i + size].decode("utf-8", errors="replace")
            i += size
        else:
            raise ValueError("unexpected protobuf wire type {}".format(wire))
    try:
        msg["payload"] = json.loads(msg.get("payload") or "null")
    except ValueError:
        msg["payload"] = None
    return msg


def _read_varint(data, i):
    n = shift = 0
    while True:
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


# ---- one speaker ----------------------------------------------------------------
class CastError(Exception):
    pass


class CastConnection:
    """A Cast session with one speaker. Use as a context manager."""

    def __init__(self, host, timeout=10):
        self.host, self.timeout = host, timeout
        self.request_id = 0
        self.sock = None

    def __enter__(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False          # speakers have self-signed certificates
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.host, CAST_PORT), timeout=self.timeout)
        self.sock = context.wrap_socket(raw, server_hostname=self.host)
        self.send("receiver-0", NS_CONNECTION, {"type": "CONNECT"})
        return self

    def __exit__(self, *exc):
        try:
            self.send("receiver-0", NS_CONNECTION, {"type": "CLOSE"})
        except OSError:
            pass
        self.sock.close()

    def send(self, destination, namespace, payload):
        self.sock.sendall(encode_message("sender-0", destination, namespace, payload))

    def request(self, destination, namespace, payload, want):
        """Send payload with a new requestId; return the first reply on namespace whose type is in want."""
        self.request_id += 1
        payload = dict(payload, requestId=self.request_id)
        self.send(destination, namespace, payload)
        return self.wait(namespace, want, self.request_id)

    def wait(self, namespace, want, request_id=None):
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            msg = self.receive()
            payload = msg.get("payload") or {}
            if msg.get("namespace") == NS_HEARTBEAT and payload.get("type") == "PING":
                self.send(msg.get("source", "receiver-0"), NS_HEARTBEAT, {"type": "PONG"})
                continue
            if msg.get("namespace") != namespace or payload.get("type") not in want:
                continue
            if request_id is not None and payload.get("requestId") not in (request_id, 0, None):
                continue
            return payload
        raise CastError("{}: no {} reply".format(self.host, "/".join(want)))

    def receive(self):
        size = struct.unpack(">I", self._read(4))[0]
        return decode_message(self._read(size))

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise CastError("{}: connection closed".format(self.host))
            data += chunk
        return data

    def status(self):
        """The receiver's status: {'volume': {'level': 0.4, 'muted': False}, 'applications': [...]}."""
        return self.request("receiver-0", NS_RECEIVER, {"type": "GET_STATUS"}, ("RECEIVER_STATUS",))["status"]

    def set_volume(self, level):
        """Set the speaker's volume, 0.0 to 1.0."""
        self.request("receiver-0", NS_RECEIVER, {"type": "SET_VOLUME", "volume": {"level": level}},
                     ("RECEIVER_STATUS",))

    def play(self, url, title="Alert", content_type="audio/mp3"):
        """Play url in the Default Media Receiver. Returns once the speaker has accepted it."""
        reply = self.request("receiver-0", NS_RECEIVER, {"type": "LAUNCH", "appId": DEFAULT_MEDIA_RECEIVER},
                             ("RECEIVER_STATUS", "LAUNCH_ERROR"))
        # A speaker that is showing something else (a Nest Hub's photo frame) first reports that app, and
        # the media player only in a later status message
        end = time.monotonic() + self.timeout
        while True:
            if reply.get("type") == "LAUNCH_ERROR":
                raise CastError("{}: could not start the media player: {}".format(self.host, reply))
            apps = [a for a in reply.get("status", {}).get("applications", [])
                    if a.get("appId") == DEFAULT_MEDIA_RECEIVER and a.get("transportId")]
            if apps:
                break
            if time.monotonic() > end:
                raise CastError("{}: media player did not start".format(self.host))
            reply = self.wait(NS_RECEIVER, ("RECEIVER_STATUS", "LAUNCH_ERROR"))
        transport = apps[0]["transportId"]
        self.send(transport, NS_CONNECTION, {"type": "CONNECT"})
        media = {"contentId": url, "contentType": content_type, "streamType": "BUFFERED",
                 "metadata": {"type": 0, "metadataType": 0, "title": title}}
        reply = self.request(transport, NS_MEDIA, {"type": "LOAD", "media": media, "autoplay": True},
                             ("MEDIA_STATUS", "LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST"))
        if reply.get("type") != "MEDIA_STATUS":
            raise CastError("{}: speaker refused the audio: {}".format(self.host, reply.get("type")))


# ---- several speakers -------------------------------------------------------------
def parse_devices(text):
    """'bedroom=192.0.2.12, study=192.0.2.8' -> {'bedroom': '192.0.2.12', 'study': ...}.
    A bare address is named after itself."""
    devices = {}
    for item in text.split(","):
        item = item.strip()
        if item:
            name, _, host = item.rpartition("=")
            devices[(name or host).strip()] = host.strip()
    return devices


MDNS = ("224.0.0.251", 5353)


def _mdns_name(data, i):
    """A DNS name at data[i] (following compression pointers) -> (name, index after it)."""
    labels, end = [], None
    for _ in range(64):
        n = data[i]
        if n >= 0xC0:
            end = end or i + 2
            i = ((n & 0x3F) << 8) | data[i + 1]
        elif n == 0:
            return ".".join(labels), end or i + 1
        else:
            labels.append(data[i + 1:i + 1 + n].decode("utf-8", "replace"))
            i += 1 + n
    raise ValueError("bad name")


def discover(timeout=3.0, service="_googlecast._tcp.local"):
    """Google Cast devices on the local network, by an mDNS query: {friendly name: (address, model)}, e.g.
    {'Bedroom': ('192.168.1.134', 'Google Home'), 'workout chrome': ('192.168.1.217', 'Chromecast')}. Listens on the mDNS port (shared
    with avahi, if it runs) for the speakers' multicast answers: they don't answer an ordinary port.
    Needs no avahi or extra packages."""
    q = struct.pack(">6H", 0, 0, 1, 0, 0, 0) + b"".join(
        bytes([len(p)]) + p.encode() for p in service.split(".")) + b"\0" + struct.pack(">HH", 12, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        s.bind(("", MDNS[1]))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(MDNS[0]) + socket.inet_aton("0.0.0.0"))
    except OSError as e:
        log.warning("google home: can't listen on the mDNS port (%s), asking from another port", e)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    names, targets, hosts, models = {}, {}, {}, {}  # instance: fn / SRV target / md, target: A address
    try:
        s.sendto(q, MDNS)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            s.settimeout(max(0.05, end - time.monotonic()))
            try:
                data, _ = s.recvfrom(9000)
            except socket.timeout:
                break
            try:
                qd, an, ns, ar = struct.unpack(">4H", data[4:12])
                i = 12
                for _ in range(qd):
                    i = _mdns_name(data, i)[1] + 4
                for _ in range(an + ns + ar):
                    rname, i = _mdns_name(data, i)
                    rtype, _, _, rlen = struct.unpack(">HHIH", data[i:i + 10])
                    i += 10
                    rdata = data[i:i + rlen]
                    if rtype == 1 and rlen == 4:                         # A
                        hosts[rname.lower()] = socket.inet_ntoa(rdata)
                    elif rtype == 33:                                    # SRV
                        targets[rname] = _mdns_name(data, i + 6)[0].lower()
                    elif rtype == 16:                                    # TXT
                        j = 0
                        while j < rlen:
                            item = rdata[j + 1:j + 1 + rdata[j]].decode("utf-8", "replace")
                            j += 1 + rdata[j]
                            if item.startswith("fn="):
                                names[rname] = item[3:]
                            elif item.startswith("md="):
                                models[rname] = item[3:]
                    i += rlen
            except (IndexError, ValueError, struct.error):
                continue
    finally:
        s.close()
    return {fn: (hosts[targets[inst]], models.get(inst, "")) for inst, fn in names.items()
            if targets.get(inst) in hosts}


def match_device(name, found):
    """The address in discover()'s found for a google_home.devices name: the device whose friendly name
    is the same (any case), else the only speaker (not a Chromecast TV stick) whose name starts with it,
    e.g. workout -> "Workout speaker". None when there is no such device."""
    name = name.lower()
    exact = [a for fn, (a, md) in found.items() if fn.lower() == name]
    if exact:
        return exact[0]
    start = [a for fn, (a, md) in found.items() if fn.lower().startswith(name) and md != "Chromecast"]
    return start[0] if len(start) == 1 else None


def spoken_seconds(text):
    """About how long Google's voice takes to say text."""
    return 2.0 + len(text) / 13.0


def _restore_volume(host, quiet, level, after, timeout):
    """After the message, put a speaker's volume back to level, unless someone changed it meanwhile."""
    time.sleep(after)
    try:
        with CastConnection(host, timeout) as cast:
            now = cast.status().get("volume", {}).get("level")
            if now is not None and abs(now - quiet) < 0.02:
                cast.set_volume(level)
    except Exception as e:
        log.warning("google home: %s: could not put the volume back to %.2f: %s", host, level, e)


def parse_hours(text):
    """'00:00-08:00' -> (0, 480) minutes after midnight; '' -> None. The end may be past midnight."""
    if not text or not text.strip():
        return None
    a, b = (p.strip() for p in text.split("-"))
    mins = [int(x.split(":")[0]) * 60 + (int(x.split(":")[1]) if ":" in x else 0) for x in (a, b)]
    return mins[0], mins[1]


def parse_volumes(text):
    """'0.3, bedroom=0.15' -> {'*': 0.3, 'bedroom': 0.15} (a bare level is for every speaker)."""
    out = {}
    for item in (text or "").split(","):
        item = item.strip()
        if item:
            name, _, level = item.rpartition("=")
            out[name.strip() or "*"] = float(level)
    return out


def say(devices, text, language="en", timeout=10, volumes=None):
    """Say text on every speaker at once (devices: {name: address}). Returns {name: None or error text}.
    volumes: {name: level} to say it at on those speakers (only ever quieter than they are); the old
    volume is put back once the message is over."""
    url = tts_url(text, language)
    results = {}
    volumes = volumes or {}

    def one(name, host):
        try:
            with CastConnection(host, timeout) as cast:
                quiet = volumes.get(name)
                old = cast.status().get("volume", {}).get("level") if quiet is not None else None
                if old is not None and old > quiet:
                    cast.set_volume(quiet)
                    threading.Thread(target=_restore_volume, args=(host, quiet, old, spoken_seconds(text) + 3,
                                                                   timeout), daemon=True).start()
                cast.play(url, title=text[:60])
            results[name] = None
        except Exception as e:
            results[name] = str(e) or e.__class__.__name__

    threads = [threading.Thread(target=one, args=item, daemon=True) for item in devices.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout * 3)
    return {name: results.get(name, "timed out") for name in devices}


class Announcer:
    """Speaks a message for the labels of an event, on its own thread so the camera never waits.
    messages: {label: text}; a label with no text (or not listed) is not announced. Labels are announced
    in the order of messages, all in one message.

    quiet_minutes: a label is announced again only after that long with no event showing it, so a cat
    sitting at the door, which makes one event after another, is announced once. Each label has its own
    clock: a raccoon is still announced while the cat sits there. 0 = announce every event.
    reminders: {label: text} said instead, at most every remind_minutes, while that label keeps showing
    up within quiet_minutes ("The cat is still at the door"); a label with no text gets no reminder.
    An event's labels that its early check already announced (cmo_watch's google_home.early) are passed
    as handled at the end of the event: their clocks move on but they are not said again, whatever
    quiet_minutes is. Thread-safe: the early check and the end of the event call it from two threads.
    announce's extra: a callable that returns a sentence to add after the message (cmo_watch's
    google_home.cute: a cute sentence about the picture), or (sentence, language) to say the whole message
    in that accent (e.g. "en-GB"), or None; it is called on the announcement's thread, and only when there
    is something to say.
    night, night_volume: quieter between those hours (see parse_hours, parse_volumes).
    find_moved: a speaker that doesn't answer is looked up by name on the network (discover) and its new
    address used; all of them are checked once at the start too."""

    def __init__(self, devices, messages, language="en", timeout=10, quiet_minutes=0, reminders=None,
                 remind_minutes=0, night="", night_volume="", find_moved=True):
        self.devices = dict(devices)
        self.messages = {k: v.strip() for k, v in messages.items() if v and v.strip()}
        self.language = language
        self.timeout = timeout
        self.quiet = quiet_minutes * 60
        self.reminders = {k: v.strip() for k, v in (reminders or {}).items() if v and v.strip()}
        self.remind = remind_minutes * 60
        self.seen = {}              # label: time.monotonic() of the last event that showed it
        self.said = {}              # label: when its message or reminder was last said
        self.lock = threading.Lock()
        self.stats = {"announced": 0, "failed": 0}
        self.on_result = None       # callable(ok_speakers, failed_speakers) after each announcement, or None
        self.night = parse_hours(night)                 # (start, end) minutes after midnight, or None
        self.night_volume = parse_volumes(night_volume)  # {name or "*": level}
        self.find_moved = find_moved
        self.last_find = 0.0
        if find_moved and self.devices:
            threading.Thread(target=self.check_devices, name="google_home_find", daemon=True).start()

    def volumes(self, now=None):
        """{speaker: level} for now: the night volumes between google_home.night's hours, else {}."""
        if not self.night or not self.night_volume:
            return {}
        now = now or datetime.now()
        m = now.hour * 60 + now.minute
        start, end = self.night
        if not (start <= m < end if start < end else m >= start or m < end):
            return {}
        return {d: self.night_volume.get(d, self.night_volume.get("*")) for d in self.devices
                if self.night_volume.get(d, self.night_volume.get("*")) is not None}

    def check_devices(self, names=None):
        """Look the speakers up by name (mDNS) and move any whose address changed (the router gave it a new
        one). Returns the names that moved."""
        self.last_find = time.monotonic()
        try:
            found = discover()
        except OSError as e:
            log.warning("google home: could not look the speakers up: %s", e)
            return []
        moved = []
        for name in names or list(self.devices):
            addr = match_device(name, found)
            if addr is None:
                log.warning("google home: %s (%s) not found on the network (found: %s)", name, self.devices[name],
                            ", ".join("{} {}".format(fn, a) for fn, (a, md) in sorted(found.items())) or "none")
            elif addr != self.devices[name]:
                log.warning("google home: %s moved from %s to %s; using the new address (update "
                            "google_home.devices)", name, self.devices[name], addr)
                self.devices[name] = addr
                moved.append(name)
        if not names:
            log.info("google home: speakers checked: %s", ", ".join("{} {}".format(n, a) for n, a in self.devices.items()))
        return moved

    @staticmethod
    def _join(texts):
        texts = [t if t[-1] in ".!?" else t + "." for t in texts]
        return " ".join(texts) if len(texts) > 1 else "".join(texts)

    def text_for(self, labels):
        """The sentence for an event's labels (a list like ['cat', 'animal']), or '' for none."""
        return self._join([t for label, t in self.messages.items() if label in labels])

    def text_now(self, labels, now=None, handled=()):
        """text_for, less the labels still within quiet_minutes of their last event (or their reminder,
        when one is due) and the handled ones. Records the event, so call it once per event (and once
        more at its end after an early announcement, with handled)."""
        now = time.monotonic() if now is None else now
        texts = []
        with self.lock:
            for label, text in self.messages.items():
                if label not in labels:
                    continue
                last = self.seen.get(label)
                self.seen[label] = now
                if label in handled:
                    continue
                if not self.quiet or last is None or now - last >= self.quiet:
                    texts.append(text)
                elif label in self.reminders and self.remind and now - self.said.get(label, now) >= self.remind:
                    texts.append(self.reminders[label])
                else:
                    continue
                self.said[label] = now
        return self._join(texts)

    def announce(self, labels, name="", handled=(), extra=None):
        """Say the message for labels now (on its own thread). handled: labels this event already
        announced early, not said again. extra: see the class."""
        text = self.text_now(labels, handled=handled)
        early = [l for l in self.messages if l in labels and l in handled]
        if early:
            log.info("google home: %s: %s already announced early", name, ", ".join(early))
        rest = [l for l in labels if l not in handled]
        if text and self.devices:
            threading.Thread(target=self._say, args=(text, name, extra), name="google_home", daemon=True).start()
        elif self.text_for(rest):
            log.info("google home: %s: quiet, %s seen less than %.0f min ago", name,
                     ", ".join(l for l in self.messages if l in rest), self.quiet / 60)
        return text

    def _say(self, text, name, extra=None):
        language = self.language
        if extra:
            try:
                more = extra() or ""
                if isinstance(more, tuple):         # (sentence, language for its accent, e.g. "en-GB")
                    more, language = more[0] or "", more[1] or language
                more = more.strip()
            except Exception as e:
                log.warning("google home: %s: no extra sentence: %s", name, e)
                more = ""
            if more and len(text) + 1 + len(more) <= TTS_MAX_CHARS:
                text = self._join([text, more])
            elif more:
                log.info("google home: %s: extra sentence left out, too long for one message: %r", name, more)
        volumes = self.volumes()
        try:
            results = say(self.devices, text, language, self.timeout, volumes)
            failed = [d for d, err in results.items() if err]
            # a speaker that doesn't answer may have a new address: look it up (at most every 5 min) and retry
            if failed and self.find_moved and time.monotonic() - self.last_find > 300:
                moved = self.check_devices(failed)
                if moved:
                    results.update(say({d: self.devices[d] for d in moved}, text, language, self.timeout, volumes))
        except Exception as e:
            self.stats["failed"] += 1
            log.warning("google home: %s: %s", name, e)
            return
        failed = {d: err for d, err in results.items() if err}
        ok = [d for d, err in results.items() if not err]
        if self.on_result:
            self.on_result(ok, sorted(failed))
        self.stats["announced" if ok else "failed"] += 1
        (log.warning if failed else log.info)("google home: %s said %r%s%s on %s%s", name, text,
                 "" if language == self.language else " in " + language,
                 " (night volume)" if volumes else "", ", ".join(ok) or "no speaker",
                 "; failed: " + "; ".join("{} ({})".format(d, e) for d, e in failed.items()) if failed else "")
