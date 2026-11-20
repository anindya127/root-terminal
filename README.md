# ROOT terminal

ESP32-S3-N16R8 voice terminal that talks to a server I own, not a vendor cloud.
Hardware: the "小智AI" bread board — INMP441 mic, MAX98357A amp, GMT130 240x240
ST7789 panel, WS2812 status LED, BOOT button.

## Architecture (v7/v8): HTTP only

Long-lived TCP kept deadlocking — writes blocked, reads never arrived, the loop
froze and audio died. Every interaction is now a short HTTP request:

```
hold BOOT   -> mic buffers to PSRAM (mono PCM16 16 kHz)
release     -> POST /utt  (raw PCM)
idle loop   -> GET /job every 1.2 s; id > 0 then GET /a/<id>.wav and play it
```

The playback task owns I2S and drains a 64 KB PSRAM ring; the main loop only
writes into the ring, so a slow speaker can never stall the network side.

## Server

`server_http.py` on port 8100 (systemd unit `root-bridge`):

| Endpoint | Purpose |
|---|---|
| `GET /job` | `{"id":N}` next clip to speak, `0` when idle |
| `GET /a/<id>.wav` | the clip, 16 kHz mono PCM16 in a WAV container |
| `POST /utt` | raw PCM body, transcribed with faster-whisper, reply queued |
| `GET /say?text=...` | queue speech (how Root talks to the device) |
| `GET /status` | counters: jobs, fetches, utterances, last heard |

Speech is synthesised locally with piper (`en_US-lessac-medium`), converted with
ffmpeg. Volume is applied on the device at 1% — this sits in a bedroom.

## Pin map

From the upstream profile `78/xiaozhi-esp32`, verified on this unit:

```
MIC  WS 4   SCK 5   SD 6
SPK  DOUT 7 BCLK 15 LRCK 16
LED  48 (WS2812)     BUTTON 0
```

Display pins are **not yet confirmed**. The panel is a 7-pin GMT130 (GND VCC SCK
SDA RES DC BLK — no CS). At boot the firmware draws `SET n` for three seconds per
candidate set, since the audio pins match two different upstream boards:

1. `41 / 42 / 40 / 45`  (zhengchen 1.54tft-wifi)
2. `47 / 21 / 40 / 45`  (bread-compact-wifi-lcd)
3. `10 / 9 / 8 / 18`    (nologo xingzhi-cube 1.54tft)
4. `21 / 47 / 40 / 45`  (swapped MOSI/CLK)

Whichever number appears on screen is the wiring; lock it in and delete the probe.

## Lessons (learned the hard way)

- A blocking I2S write inside the socket read loop kills the connection: the
  device stops reading, the peer resets, audio is lost. Separate task, always.
- `WiFiClient` stays unusable after a failed `connect()` until `stop()` is called.
- Counting bytes you *asked* the mic for instead of bytes it *gave* corrupts the
  upload silently.
- Verify on the wire, not in the log: the server said "sent" while the board
  received nothing.

## Build

```
pio run
esptool --chip esp32s3 --port COM4 --baud 460800 write_flash \
  --flash_mode dio --flash_freq 80m --flash_size 16MB \
  0x0 bootloader.bin 0x8000 partitions.bin 0x10000 firmware.bin
```
