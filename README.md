# ROOT terminal

Firmware for an ESP32-S3-N16R8 voice terminal that talks to a server I own —
not to a vendor cloud. The hardware is the "小智AI" / XiaoZhi bread board:

| Part | Value |
|---|---|
| MCU | ESP32-S3-N16R8 (16 MB flash, 8 MB PSRAM) |
| Mic | INMP441 I2S |
| Amp | MAX98357A (I2S) |
| Display | GMT130-V1.0, ST7789 240x240 IPS, 7-pin (no CS) |
| Speaker | 4 Ω 3 W 3070 |

## Pin map

Taken from the upstream board profile
`78/xiaozhi-esp32 · main/boards/bread-compact-wifi-lcd/config.h`, then verified
on real hardware — the mic and amp were proven with live levels and audible
tones before anything else was written.

```
MIC  WS 4   SCK 5   DIN 6
SPK  DOUT 7 BCLK 15 LRCK 16
LCD  MOSI 47 CLK 21 DC 40 RST 45 BL 42   CS: none (tied low)
```

## Wire protocol

Newline-delimited JSON over TCP, port 8099, then raw PCM:

```
device -> server   {"type":"hello","device":"xiaozhi-s3","v":2,"rssi":-41}
server -> device   {"type":"welcome","text":"root here"}
server -> device   {"type":"audio","bytes":4096,"rate":16000,"seq":7}
                   <4096 bytes of PCM16 LE, 16 kHz mono>
```

Audio is sent in 4 KB frames, each announced before it is written. A single
100 KB block made the board reset the connection mid-transfer; 4 KB frames
stopped it. The device reads a whole frame, then hands it to I2S in one write —
dribbling 256 bytes at a time throttled playback to a fifth of real time.

## Server

`bridge_voice.py` synthesises speech with piper (offline, local voice model),
converts to 16 kHz mono PCM with ffmpeg, and streams it in paced frames.

## Status

- [x] custom firmware runs on the board
- [x] mic and amp verified on hardware
- [x] joy: link to server, speech out, smooth, whisper-quiet (1% gain)
- [ ] display bring-up (pins wired, driver compiled, not yet confirmed on a lit panel)
- [ ] Opus instead of raw PCM
- [ ] voice in: mic -> server -> transcription
- [ ] music playback

## Build / flash

```
pio run
esptool --chip esp32s3 --port COM4 --baud 460800 write_flash \
  --flash_mode dio --flash_freq 80m --flash_size 16MB \
  0x0 bootloader.bin 0x8000 partitions.bin 0x10000 firmware.bin
```

Wi-Fi is provisioned by captive portal (`Root-ESP32-Setup`); credentials never
leave the device.
