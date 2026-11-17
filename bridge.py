#!/usr/bin/env python3
"""Root bridge: WebSocket endpoint for the XiaoZhi board.

Stage 2 of the voice terminal. For now it answers a device hello, echoes any
text back, and logs everything so we can see the device arrive. Audio and the
Hermes brain come next, behind the same socket.
"""
import asyncio
import json
import logging
import time

BIND = "0.0.0.0"
PORT = 8099

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("/home/anindya/xiaozhi-root/bridge.log"), logging.StreamHandler()],
)
log = logging.getLogger("root-bridge")


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
            log.info("RX %s", text[:400])
            try:
                msg = json.loads(text)
            except Exception:
                msg = {"raw": text}

            reply = {"type": "ack", "t": int(time.time())}
            if msg.get("type") == "hello":
                reply = {"type": "welcome", "text": "root here", "t": int(time.time())}
            elif msg.get("type") == "say":
                reply = {"type": "speak", "text": "I heard: " + str(msg.get("text", ""))[:80]}

            payload = (json.dumps(reply) + "\n").encode()
            writer.write(payload)
            await writer.drain()
            log.info("TX %s", reply)
    except Exception as exc:
        log.warning("device error: %s", exc)
    finally:
        log.info("device disconnected %s", peer)
        writer.close()


async def main():
    server = await asyncio.start_server(handle, BIND, PORT)
    log.info("root-bridge listening on %s:%d", BIND, PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
