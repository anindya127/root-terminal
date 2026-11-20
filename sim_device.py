#!/usr/bin/env python3
"""Simulate the device: send an utterance exactly as the firmware would, then
read back what the bridge answers. Proves transcription + reply without hardware."""
import asyncio
import json
import wave
import subprocess
import os

HOST, PORT = "127.0.0.1", 8099
PIPER = os.path.expanduser("~/.local/bin/piper")
VOICE = os.path.expanduser("~/piper-voices/en_US-lessac-medium.onnx")
FFMPEG = os.path.expanduser("~/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg")
PHRASE = "Hello Root, please turn the lights on and tell me the time."


def synthesise() -> bytes:
    env = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""))
    subprocess.run([PIPER, "-m", VOICE, "-f", "/tmp/utt.wav"], input=PHRASE.encode(), check=True, timeout=120, env=env)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", "/tmp/utt.wav",
                    "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "/tmp/utt.pcm"],
                   check=True, timeout=120)
    with open("/tmp/utt.pcm", "rb") as fh:
        return fh.read()


async def main():
    pcm = synthesise()
    print("utterance: %d bytes (%.1f s)" % (len(pcm), len(pcm) / 2 / 16000))
    r, w = await asyncio.open_connection(HOST, PORT)
    w.write(b'{"type":"hello","device":"simulator","v":4}\n')
    await w.drain()

    step = 3200
    for off in range(0, len(pcm), step):
        part = pcm[off:off + step]
        w.write(('{"type":"mic","bytes":%d}\n' % len(part)).encode())
        w.write(part)
        await w.drain()
        await asyncio.sleep(0.06)

    w.write(('{"type":"end","bytes":%d}\n' % len(pcm)).encode())
    await w.drain()

    deadline = asyncio.get_event_loop().time() + 45
    audio = 0
    while asyncio.get_event_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(r.readline(), timeout=20)
        except asyncio.TimeoutError:
            break
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        try:
            msg = json.loads(text)
        except Exception:
            msg = {"raw": text[:60]}
        print("<-", msg if "raw" not in msg else msg)
        if msg.get("type") == "audio":
            audio = int(msg.get("bytes", 0))
            await r.readexactly(audio)
            print("<- [received %d bytes of speech]" % audio)
            break
    print("AUDIO_BYTES", audio)


asyncio.run(main())
