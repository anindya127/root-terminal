#!/usr/bin/env python3
"""ROOT terminal server — HTTP only.

The board polls for work, fetches audio as a WAV file, and posts utterances.
There is no long-lived socket, so nothing can deadlock: every interaction is a
short request that either completes or fails visibly.

Endpoints
    GET  /job        -> {"id": N, "text": "..."} (id 0 = nothing to say)
    GET  /a/<id>.wav -> the clip as a WAV file
    POST /utt        -> raw PCM16 16 kHz mono body; transcribes and queues a reply
    GET  /status     -> counters, for debugging
    GET  /say?text=  -> queue speech (so Root can talk from the VPS)
"""
import io
import json
import logging
import os
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

PORT = 8100
PIPER = os.path.expanduser("~/.local/bin/piper")
VOICE = os.path.expanduser("~/piper-voices/en_US-lessac-medium.onnx")
FFMPEG = os.path.expanduser("~/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg")
HERE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(HERE, "bridge.log")), logging.StreamHandler()],
)
log = logging.getLogger("root-http")

_lock = threading.Lock()
_audio = {}          # id -> wav bytes
_seq = {"n": 0}
_pending = []        # ids waiting to be spoken, newest last
_stats = {"jobs": 0, "audio_fetched": 0, "utterances": 0, "heard": [], "last_post": 0}
_whisper = None


# ------------------------------------------------------------------ speech ----

def synth(text: str) -> bytes:
    env = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""))
    subprocess.run([PIPER, "-m", VOICE, "-f", "/tmp/h_tts.wav"], input=text.encode(), check=True, timeout=120, env=env)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", "/tmp/h_tts.wav",
                    "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "/tmp/h_tts.pcm"],
                   check=True, timeout=120)
    with open("/tmp/h_tts.pcm", "rb") as fh:
        pcm = fh.read()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def queue_speech(text: str) -> int:
    wav = synth(text)
    with _lock:
        _seq["n"] += 1
        key = _seq["n"]
        _audio[key] = wav
        _pending.append(key)
        for old in [k for k in _audio if k < key - 6]:
            _audio.pop(old, None)
    log.info("queued speech id=%d (%d wav bytes): %s", key, len(wav), text[:60])
    return key


def transcribe(pcm: bytes) -> str:
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        log.info("whisper ready")
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    audio = np.clip(audio * 12.0, -1.0, 1.0)
    segs, _ = _whisper.transcribe(audio, beam_size=1, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segs).strip()


# ------------------------------------------------------------------ server ----

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)

        if u.path == "/job":
            log.info("job poll from %s", self.client_address[0])
            with _lock:
                if _pending:
                    key = _pending.pop(0)
                    _stats["jobs"] += 1
                    self._json({"id": key, "text": ""})
                else:
                    self._json({"id": 0})
            return

        if u.path.startswith("/a/"):
            try:
                key = int(u.path.split("/a/")[1].split(".")[0])
            except Exception:
                self._json({"error": "bad id"}, 400); return
            with _lock:
                body = _audio.get(key)
                if body:
                    _stats["audio_fetched"] += 1
            if not body:
                self._json({"error": "gone"}, 404); return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log.info("served audio %d (%d bytes)", key, len(body))
            return

        if u.path == "/say":
            text = (parse_qs(u.query).get("text") or ["Hello sir."])[0]
            key = queue_speech(text)
            self._json({"queued": key})
            return

        if u.path == "/status":
            with _lock:
                self._json({"stats": {**_stats, "pending": len(_pending), "seq": _seq["n"]}})
            return

        if u.path == "/ping":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/utt":
            self._json({"error": "not found"}, 404); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        pcm = self.rfile.read(length) if length else b""
        dur = len(pcm) / 2 / 16000
        rms = float(np.sqrt((np.frombuffer(pcm, dtype=np.int16).astype(np.float32) ** 2).mean())) if len(pcm) > 1 else 0.0
        log.info("utterance: %d bytes (%.1fs) rms %.0f", len(pcm), dur, rms)
        with _lock:
            _stats["utterances"] += 1
            _stats["last_post"] = int(time.time())

        text = ""
        if len(pcm) > 16000 and rms >= 40:
            try:
                text = transcribe(pcm)
            except Exception as exc:
                log.warning("transcribe failed: %s", exc)
        log.info("HEARD: %s", text or "<nothing>")
        print("HEARD:", text, flush=True)
        with _lock:
            _stats["heard"] = (_stats["heard"] + [text])[-5:]

        reply = ("I heard you say: " + text) if text else "I could not make that out. Try again, closer to the microphone."
        queue_speech(reply)
        self._json({"heard": text})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("root-http listening on :%d", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
