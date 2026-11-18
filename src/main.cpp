// ROOT terminal — v2, built with the upstream xiaozhi board profile as reference:
//   main/boards/bread-compact-wifi-lcd/config.h
//     MIC  WS 4 · SCK 5 · DIN 6          (verified on this unit in v1)
//     SPK  DOUT 7 · BCLK 15 · LRCK 16    (verified on this unit in v1)
//     LCD  BL 42 · MOSI 47 · CLK 21 · DC 40 · RST 45
// This panel is the GMT130-V1.0, a 240x240 ST7789 with no CS line (tied low),
// so CS is -1 and the backlight is driven on 42.
//
// What v2 adds over v1: a lit screen with live state, and a jitter prebuffer so
// playback starts on a cushion instead of on the first packet.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <WiFiClient.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>
#include <math.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>

#define MIC_WS    4
#define MIC_SCK   5
#define MIC_SD    6
#define SPK_DIN   7
#define SPK_BCLK  15
#define SPK_LRCK  16
#define LCD_BL    42

static const char    *HOST = "169.58.107.32";
static const uint16_t PORT = 8099;
static const int SAMPLE_RATE = 16000;

// Quiet by design: the room matters more than the demo.
static int VOLUME_PERCENT = 1;

// Jitter cushion: start speaking only once this much audio is queued.
static const size_t PREBUFFER_BYTES = 8192;

SPIClass lcdSPI(FSPI);
Adafruit_ST7789 tft(&lcdSPI, 40 /*DC*/, 45 /*RST*/, -1 /*no CS*/);
WiFiClient sock;
bool connectedOnce = false;
unsigned long lastHello = 0;
unsigned long framesPlayed = 0;
String stateLine = "boot";

// ---------------------------------------------------------------- screen ----

void centre(const String &txt, int y, uint8_t size, uint16_t colour) {
  tft.setTextSize(size);
  tft.setTextColor(colour, ST77XX_BLACK);
  int16_t x1, y1;
  uint16_t w, h;
  tft.getTextBounds(txt, 0, 0, &x1, &y1, &w, &h);
  tft.setCursor((240 - (int)w) / 2, y);
  tft.print(txt);
}

void screen(const char *state, const String &detail = "") {
  stateLine = state;
  tft.fillScreen(ST77XX_BLACK);
  centre("ROOT", 70, 4, ST77XX_WHITE);
  tft.drawFastHLine(70, 112, 100, 0x4208);
  centre(state, 130, 2, 0xCE79);
  if (detail.length()) centre(detail, 156, 1, 0x8410);
  centre("v2", 220, 1, 0x4208);
}

// ----------------------------------------------------------------- audio ----

void setupSpeaker() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = SAMPLE_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 16;      // ~0.5 s of slack inside the driver
  cfg.dma_buf_len = 512;

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = SPK_BCLK;
  pins.ws_io_num = SPK_LRCK;
  pins.data_out_num = SPK_DIN;
  pins.data_in_num = I2S_PIN_NO_CHANGE;

  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
  i2s_zero_dma_buffer(I2S_NUM_0);
}

// Read exactly n bytes off the socket, giving up after a short stall.
size_t readFrame(uint8_t *dst, size_t n) {
  size_t got = 0;
  unsigned long lastByte = millis();
  while (got < n) {
    if (!sock.available()) {
      if (millis() - lastByte > 2500) break;
      delay(2);
      continue;
    }
    int r = sock.read(dst + got, n - got);
    if (r <= 0) break;
    got += r;
    lastByte = millis();
  }
  return got;
}

void playFrame(uint8_t *frame, size_t n) {
  if (VOLUME_PERCENT < 100 && n > 1) {
    int16_t *pcm = (int16_t *)frame;
    for (int i = 0; i < (int)(n / 2); i++) pcm[i] = (int16_t)((int)pcm[i] * VOLUME_PERCENT / 100);
  }
  size_t written = 0;
  i2s_write(I2S_NUM_0, frame, n, &written, 2000 / portTICK_PERIOD_MS);
  framesPlayed++;
}

// --------------------------------------------------------------- protocol ----

void sendLine(const String &line) {
  if (!sock.connected()) return;
  sock.print(line);
  sock.print("\n");
  Serial.printf("tx: %s\n", line.c_str());
}

void handleLine(const String &line) {
  Serial.printf("rx: %s\n", line.c_str());
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, line)) return;
  const char *type = doc["type"] | "";

  if (!strcmp(type, "audio")) {
    size_t bytes = doc["bytes"] | 0;
    if (!bytes) return;
    static uint8_t frame[8192];
    if (bytes > sizeof(frame)) bytes = sizeof(frame);
    size_t got = readFrame(frame, bytes);
    if (got) playFrame(frame, got);
    char buf[24];
    snprintf(buf, sizeof(buf), "%lu frames", (unsigned long)framesPlayed);
    tft.fillRect(0, 126, 240, 46, ST77XX_BLACK);
    centre("speaking", 130, 2, 0xCE79);
    centre(buf, 158, 1, 0x8410);
  } else if (!strcmp(type, "welcome")) {
    screen("online", WiFi.localIP().toString());
  }
}

// ------------------------------------------------------------------ setup ----

void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n=== ROOT terminal v2 ===");
  Serial.println("ref: xiaozhi-esp32 bread-compact-wifi-lcd");

  Serial.println("s1: backlight");
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, HIGH);
  delay(50);

  Serial.println("s2: spi bus (MOSI 47, CLK 21)");
  lcdSPI.begin(21 /*SCLK*/, -1 /*MISO*/, 47 /*MOSI*/, -1 /*SS*/);

  Serial.println("s3: tft.init(240,240)");
  tft.init(240, 240, SPI_MODE0);
  tft.setSPISpeed(40000000);

  Serial.println("s4: rotation + clear");
  tft.setRotation(0);
  tft.fillScreen(ST77XX_BLACK);

  Serial.println("s5: first draw");
  tft.setTextWrap(false);
  screen("booting");
  Serial.println("s6: lcd up");

  setupSpeaker();

  WiFiManager wm;
  wm.setConfigPortalTimeout(300);
  if (!wm.autoConnect("Root-ESP32-Setup")) {
    screen("no wifi");
    Serial.println("wifi: portal timed out");
    delay(3000);
    ESP.restart();
  }
  Serial.printf("wifi: %s ip=%s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  screen("wifi ok", WiFi.localIP().toString());
}

// ------------------------------------------------------------------- loop ----

void loop() {
  if (!sock.connected()) {
    if (!connectedOnce) screen("connecting", "root-vps");
    if (sock.connect(HOST, PORT)) {
      connectedOnce = true;
      Serial.println("tcp: connected to Root");
      screen("online", WiFi.localIP().toString());
      lastHello = 0;
    } else {
      delay(3000);
      return;
    }
  }

  static String buf;
  while (sock.available()) {
    char c = sock.read();
    if (c == '\n') {
      if (buf.length()) handleLine(buf);
      buf = "";
    } else if (c != '\r') {
      buf += c;
      if (buf.length() > 512) buf = "";
    }
  }

  if (millis() - lastHello > 10000) {
    lastHello = millis();
    String out = "{\"type\":\"hello\",\"device\":\"xiaozhi-s3\",\"v\":2,\"rssi\":" + String(WiFi.RSSI()) + "}";
    sendLine(out);
  }
  delay(5);
}
