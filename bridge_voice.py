#!/usr/bin/env python3
"""Root bridge — now with a voice.

Same wire protocol as stage 2 (newline JSON over TCP), plus one new message:
    {"type":"speak","text":"..."}  server -> device   (device speaks it)
The server synthesises with piper locally, converts to 16 kHz mono PCM16 with
ffmpeg, base64s it, and sends {"type":"speak","pcm":"<base64>"} back.
"""
import asyncio
import base64
import json
import logging
import os
import subprocess
import time

BIND = "0.0.0.0"
PORT = 8099
PIPER = os.path.expanduser("~/.local/bin/piper")
VOICE = os.path.expanduser("~/piper-voices/en_US-lessac-medium.onnx")
FFMPEG = os.path.expanduser("~/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg")
GREETING = "Hello sir. This is Root, speaking from your desk."
# The board reconnects every time its TCP session drops, and each reconnect used to
# replay the greeting: 49 connects produced 47 greetings overnight, which is exactly
# the "echo" that was heard. Greet once per server run, never again.
GREET_ONCE = {"done": False}
DEVICE_WRITER = {"w": None}
DEVICES = {}          # peer ip -> newest writer for that device

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(os.path.dirname(__file__), "bridge.log")),
              logging.StreamHandler()],
)
log = logging.getLogger("root-bridge")


def synth_pcm(text: str) -> bytes:
    """piper -> wav -> 16 kHz mono PCM16."""
    wav = "/tmp/root_tts.wav"
    pcm = "/tmp/root_tts.pcm"
    env = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""))
    subprocess.run([PIPER, "-m", VOICE, "-f", wav], input=text.encode(), check=True, timeout=120, env=env)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", wav,
                    "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", pcm],
                   check=True, timeout=120)
    with open(pcm, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------- hearing ----
import struct

MIC_RATE = 16000
MIC_BUF = bytearray()
MIC_SECONDS = 2.0
_whisper = None


def _load_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        log.info("whisper loaded")
    return _whisper


def _rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm[: len(pcm) // 2 * 2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


UTTER = bytearray()


async def ingest_mic(pcm: bytes) -> None:
    """Collect what the device sends while the button is held."""
    global UTTER
    UTTER += pcm


def _transcribe(chunk: bytes) -> str:
    import numpy as np
    model = _load_whisper()
    audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
    audio = np.clip(audio * 12.0, -1.0, 1.0)
    segs, _info = model.transcribe(audio, beam_size=1, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segs).strip()


async def finish_utterance(writer) -> None:
    """Button released: transcribe, show it on the screen, answer out loud."""
    global UTTER
    chunk = bytes(UTTER)
    UTTER = bytearray()
    if len(chunk) < 8000:
        await send(writer, {"type": "state", "state": "idle"})
        return
    level = _rms(chunk)
    log.info("utterance: %d bytes, rms %.0f", len(chunk), level)
    if level < 40:
        await send(writer, {"type": "text", "role": "you", "text": "(too quiet)"})
        await send(writer, {"type": "state", "state": "idle"})
        return

    await send(writer, {"type": "state", "state": "thinking"})
    text = await asyncio.get_running_loop().run_in_executor(None, _transcribe, chunk)
    log.info("HEARD: %s", text or "<nothing>")
    print("HEARD:", text, flush=True)
    await send(writer, {"type": "text", "role": "you", "text": text or "(nothing)"})

    reply = "I heard you say: " + (text or "nothing I could make out")
    await send(writer, {"type": "state", "state": "speaking"})
    try:
        pcm = await asyncio.get_running_loop().run_in_executor(None, synth_pcm, reply)
        key = store_audio(pcm)
        await send(writer, {"type": "pull", "id": key, "bytes": len(pcm)})
    except Exception as exc:
        log.warning("reply tts failed: %s", exc)
    await send(writer, {"type": "state", "state": "idle"})


# ------------------------------------------------------------ audio server ----
# Audio is fetched over HTTP instead of pushed through the control socket: large
# binary blocks on the long-lived TCP socket were arriving as nothing at all,
# while small control lines were fine. A plain GET is bounded and verifiable.
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTTP_PORT = 8100
AUDIO_STORE = {}          # id -> (pcm bytes, wav bytes)
AUDIO_SEQ = {"n": 0}


def _wav_bytes(pcm: bytes) -> bytes:
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


class AudioHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/a/"):
            try:
                key = int(self.path.split("/a/")[1].split(".")[0])
            except Exception:
                self.send_error(400); return
            item = AUDIO_STORE.get(key)
            if not item:
                self.send_error(404); return
            body = item[1]
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log.info("http: served audio %d (%d bytes)", key, len(body))
            return
        if self.path == "/ping":
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"ok"); return
        self.send_error(404)


def start_http():
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), AudioHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("audio server on :%d", HTTP_PORT)


def store_audio(pcm: bytes) -> int:
    AUDIO_SEQ["n"] += 1
    key = AUDIO_SEQ["n"]
    AUDIO_STORE[key] = (pcm, _wav_bytes(pcm))
    for old in [k for k in AUDIO_STORE if k < key - 4]:
        AUDIO_STORE.pop(old, None)
    return key


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    ip = peer[0] if peer else "?"
    log.info("device connected from %s", peer)
    if ip != "127.0.0.1":
        old = DEVICES.get(ip)
        if old is not None and old is not writer:
            try:
                old.close()          # half-dead sockets piled up and stole the audio
                log.info("closed previous socket for %s", ip)
            except Exception:
                pass
        DEVICES[ip] = writer
        DEVICE_WRITER["w"] = writer
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            log.info("RX %s", text[:300])
            try:
                msg = json.loads(text)
            except Exception:
                msg = {}

            if msg.get("type") == "mic":
                n = int(msg.get("bytes", 0))
                if n > 0:
                    try:
                        pcm = await reader.readexactly(n)
                        await ingest_mic(pcm)
                    except Exception as exc:
                        log.warning("mic read failed: %s", exc)
                continue

            if msg.get("type") == "end":
                await finish_utterance(writer)
                continue

            if msg.get("type") == "hello" and "device" in msg:
                DEVICE_WRITER["w"] = writer
                log.info("device writer registered (%s)", msg.get("device"))

            if msg.get("type") == "hello":
                if not GREET_ONCE.setdefault("welcomed", False):
                    GREET_ONCE["welcomed"] = True
                    await send(writer, {"type": "welcome", "text": "root here"})
                # speak once per connection so the user hears the link working
                if GREET_ONCE["done"]:
                    await send(writer, {"type": "welcome", "text": "quiet"})
                    continue
                try:
                    GREET_ONCE["done"] = True
                    pcm = await asyncio.get_running_loop().run_in_executor(None, synth_pcm, GREETING)
                    log.info("sending speech: %d pcm bytes", len(pcm))
                    key = store_audio(pcm)
                    await send(writer, {"type": "pull", "id": key, "bytes": len(pcm)})
                except Exception as exc:
                    log.warning("tts failed: %s", exc)
            elif msg.get("type") == "say":
                try:
                    pcm = await asyncio.get_running_loop().run_in_executor(None, synth_pcm, str(msg.get("text", "")))
                    live = [w for i, w in DEVICES.items() if not i.startswith("127.")]
                    target = (live[-1] if live else DEVICE_WRITER["w"]) or writer
                    try:
                        peer = target.get_extra_info("peername")
                    except Exception:
                        peer = None
                    log.info("say -> %s (%d pcm bytes)", peer, len(pcm))
                    key = store_audio(pcm)
                    sent_to = 0
                    for i, w in list(DEVICES.items()):
                        if i.startswith("127."):
                            continue
                        try:
                            await send(w, {"type": "pull", "id": key, "bytes": len(pcm)})
                            sent_to += 1
                        except Exception:
                            pass
                    if not sent_to:
                        await send(writer, {"type": "pull", "id": key, "bytes": len(pcm)})
                    log.info("pull id=%d (%d pcm) -> %d socket(s)", key, len(pcm), sent_to)
                except Exception as exc:
                    log.warning("tts failed: %s", exc)
            else:
                await send(writer, {"type": "ack", "t": int(time.time())})
    except Exception as exc:
        log.warning("device error: %s", exc)
    finally:
        log.info("device disconnected %s", peer)
        if DEVICES.get(ip) is writer:
            DEVICES.pop(ip, None)
            if DEVICE_WRITER["w"] is writer:
                DEVICE_WRITER["w"] = next(iter(DEVICES.values()), None)
        writer.close()


CHUNK = 4096

async def send_audio(writer, pcm: bytes):
    """Chunked: a JSON header per frame, then that frame's bytes.

    One 100 KB block made the board reset the connection mid-transfer, so frames
    stay small and each one is announced separately.
    """
    total = len(pcm)
    for off in range(0, total, CHUNK):
        part = pcm[off:off + CHUNK]
        writer.write((json.dumps({"type": "audio", "bytes": len(part), "rate": 16000,
                                  "seq": off // CHUNK}) + "\n").encode())
        writer.write(part)
        await writer.drain()
        await asyncio.sleep(0.105)
    log.info("speech sent in %d frames (%d bytes)", (total + CHUNK - 1) // CHUNK, total)


async def send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()


async def main():
    start_http()
    server = await asyncio.start_server(handle, BIND, PORT)
    log.info("root-bridge listening on %s:%d", BIND, PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
