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


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    log.info("device connected from %s", peer)
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

            if msg.get("type") == "hello":
                await send(writer, {"type": "welcome", "text": "root here"})
                # speak once per connection so the user hears the link working
                try:
                    pcm = await asyncio.get_running_loop().run_in_executor(None, synth_pcm, GREETING)
                    log.info("sending speech: %d pcm bytes", len(pcm))
                    await send_audio(writer, pcm)
                except Exception as exc:
                    log.warning("tts failed: %s", exc)
            elif msg.get("type") == "say":
                try:
                    pcm = await asyncio.get_running_loop().run_in_executor(None, synth_pcm, str(msg.get("text", "")))
                    await send_audio(writer, pcm)
                except Exception as exc:
                    log.warning("tts failed: %s", exc)
            else:
                await send(writer, {"type": "ack", "t": int(time.time())})
    except Exception as exc:
        log.warning("device error: %s", exc)
    finally:
        log.info("device disconnected %s", peer)
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
        await asyncio.sleep(0.11)
    log.info("speech sent in %d frames (%d bytes)", (total + CHUNK - 1) // CHUNK, total)


async def send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()


async def main():
    server = await asyncio.start_server(handle, BIND, PORT)
    log.info("root-bridge listening on %s:%d", BIND, PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
