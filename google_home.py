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
    """'bedroom=192.168.1.122, study=192.168.1.108' -> {'bedroom': '192.168.1.122', 'study': ...}.
    A bare address is named after itself."""
    devices = {}
    for item in text.split(","):
        item = item.strip()
        if item:
            name, _, host = item.rpartition("=")
            devices[(name or host).strip()] = host.strip()
    return devices


def say(devices, text, language="en", timeout=10):
    """Say text on every speaker at once (devices: {name: address}). Returns {name: None or error text}."""
    url = tts_url(text, language)
    results = {}

    def one(name, host):
        try:
            with CastConnection(host, timeout) as cast:
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
    quiet_minutes is. Thread-safe: the early check and the end of the event call it from two threads."""

    def __init__(self, devices, messages, language="en", timeout=10, quiet_minutes=0, reminders=None,
                 remind_minutes=0):
        self.devices = devices
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

    def announce(self, labels, name="", handled=()):
        """Say the message for labels now (on its own thread). handled: labels this event already
        announced early, not said again."""
        text = self.text_now(labels, handled=handled)
        early = [l for l in self.messages if l in labels and l in handled]
        if early:
            log.info("google home: %s: %s already announced early", name, ", ".join(early))
        rest = [l for l in labels if l not in handled]
        if text and self.devices:
            threading.Thread(target=self._say, args=(text, name), name="google_home", daemon=True).start()
        elif self.text_for(rest):
            log.info("google home: %s: quiet, %s seen less than %.0f min ago", name,
                     ", ".join(l for l in self.messages if l in rest), self.quiet / 60)
        return text

    def _say(self, text, name):
        try:
            results = say(self.devices, text, self.language, self.timeout)
        except Exception as e:
            self.stats["failed"] += 1
            log.warning("google home: %s: %s", name, e)
            return
        failed = {d: err for d, err in results.items() if err}
        ok = [d for d, err in results.items() if not err]
        if self.on_result:
            self.on_result(ok, sorted(failed))
        self.stats["announced" if ok else "failed"] += 1
        (log.warning if failed else log.info)("google home: %s said %r on %s%s", name, text, ", ".join(ok) or "no speaker",
                 "; failed: " + "; ".join("{} ({})".format(d, e) for d, e in failed.items()) if failed else "")
