// ROOT terminal — v7, HTTP-only.
//
// Long-lived TCP kept deadlocking: writes blocked, reads never arrived, the loop
// froze and audio died. Every interaction is now a short HTTP request:
//
//   hold BOOT  -> mic buffers to PSRAM
//   release    -> POST /utt (raw PCM16 16k mono)
//   idle loop  -> GET /job every second; id>0 then GET /a/<id>.wav and play it
//
// The playback task owns I2S; the main loop only feeds a ring buffer.
//
// Pins verified on this unit: MIC 4/5/6 · SPK 7/15/16 · LED 48 (WS2812) · BTN 0
// LCD 47/21/40/45 + BL 42 (GMT130 ST7789 240x240, no CS) — still unconfirmed.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <HTTPClient.h>
#include <ArduinoOTA.h>
#include <driver/i2s.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Adafruit_NeoPixel.h>

#define MIC_WS    4
#define MIC_SCK   5
#define MIC_SD    6
#define SPK_DIN   7
#define SPK_BCLK  15
#define SPK_LRCK  16
#define LCD_BL    42
#define LED_PIN   48
#define BTN_PIN   0
#define OTA_HOST  "root-terminal"

static const char    *HOST = "169.58.107.32";
static const uint16_t HTTP_PORT = 8100;
static const int SAMPLE_RATE = 16000;
static const int VOLUME_PERCENT = 1;
static const size_t MIC_BLOCK = 3200;          // 100 ms of stereo -> 1600 B mono
static const size_t MAX_UTTER = 480000;        // 15 s ceiling on one press

SPIClass lcdSPI(FSPI);
Adafruit_ST7789 tft(&lcdSPI, 40, 45, -1);
Adafruit_NeoPixel led(1, LED_PIN, NEO_GRB + NEO_KHZ800);

// ------------------------------------------------------------------ ring ----

#define RING_BYTES 65536
static uint8_t *ringBuf = nullptr;
static volatile uint32_t ringHead = 0, ringTail = 0;
static inline uint32_t ringUsed() { return (ringHead + RING_BYTES - ringTail) % RING_BYTES; }

static void ringWrite(uint8_t b) {
  uint32_t next = (ringHead + 1) % RING_BYTES;
  if (next == ringTail) return;
  ringBuf[ringHead] = b;
  ringHead = next;
}

static void playbackTask(void *) {
  static uint8_t chunk[1024];
  for (;;) {
    if (ringUsed() < 256) { vTaskDelay(5 / portTICK_PERIOD_MS); continue; }
    size_t n = ringUsed();
    if (n > sizeof(chunk)) n = sizeof(chunk);
    n &= ~1u;
    for (size_t i = 0; i < n; i++) chunk[i] = ringBuf[(ringTail + i) % RING_BYTES];
    ringTail = (ringTail + n) % RING_BYTES;
    if (VOLUME_PERCENT < 100) {
      int16_t *pcm = (int16_t *)chunk;
      for (int k = 0; k < (int)(n / 2); k++) pcm[k] = (int16_t)((int)pcm[k] * VOLUME_PERCENT / 100);
    }
    size_t written = 0;
    i2s_write(I2S_NUM_0, chunk, n, &written, portMAX_DELAY);
  }
}

// ---------------------------------------------------------------- screen ----

// ---- display pin probe -------------------------------------------------------
// The panel's wiring is not documented for this PCB and the audio pins match two
// different upstream boards, so at boot each candidate set is drawn for three
// seconds. Whichever one lights up is the answer; the app then uses the first.
struct DispPins { int8_t mosi, clk, dc, rst, bl; };
static const DispPins DISP_CANDIDATES[] = {
  {41, 42, 40, 45, -1},   // zhengchen 1.54tft-wifi
  {47, 21, 40, 45, 42},   // bread-compact-wifi-lcd
  {10,  9,  8, 18, -1},   // nologo xingzhi-cube 1.54tft
  {21, 47, 40, 45, 42},   // swapped MOSI/CLK
  {41, 42, 45, 40, -1},   // zhengchen with DC/RST swapped
  {40, 45, 41, 42, -1},   // group shifted down
  {21, 47, 45, 40, 42},   // swapped both
  {42, 41, 40, 45, -1},   // SDA/SCL reversed
};
static const int DISP_N = sizeof(DISP_CANDIDATES) / sizeof(DISP_CANDIDATES[0]);

void probeDisplay() {
  for (int i = 0; i < DISP_N; i++) {
    const DispPins &p = DISP_CANDIDATES[i];
    if (p.bl >= 0) { pinMode(p.bl, OUTPUT); digitalWrite(p.bl, HIGH); }
    lcdSPI.end();
    lcdSPI.begin(p.clk, -1, p.mosi, -1);
    tft.init(240, 240, SPI_MODE0);
    tft.setRotation(0);
    tft.fillScreen(ST77XX_BLACK);
    tft.setTextWrap(false);
    tft.setTextColor(ST77XX_WHITE, ST77XX_BLACK);
    tft.setTextSize(5);
    tft.setCursor(24, 80);
    tft.printf("SET %d", i + 1);
    tft.setTextSize(2);
    tft.setCursor(24, 150);
    tft.printf("%d/%d/%d", p.mosi, p.clk, p.dc);
    Serial.printf("probe: set %d (mosi %d clk %d dc %d rst %d)\n", i + 1, p.mosi, p.clk, p.dc, p.rst);
    delay(3000);
  }
  lcdSPI.end();
  lcdSPI.begin(21, -1, 47, -1);       // default for the running app
  tft.init(240, 240, SPI_MODE0);
  tft.setRotation(0);
}


void centre(const String &txt, int y, uint8_t size, uint16_t colour) {
  tft.setTextSize(size);
  tft.setTextColor(colour, ST77XX_BLACK);
  int16_t x1, y1; uint16_t w, h;
  tft.getTextBounds(txt, 0, 0, &x1, &y1, &w, &h);
  int x = (240 - (int)w) / 2;
  if (x < 4) x = 4;
  tft.setCursor(x, y);
  tft.print(txt);
}

void setLed(uint8_t r, uint8_t g, uint8_t b) {
  led.setPixelColor(0, led.Color(r, g, b));
  led.show();
}

void drawChrome(const char *stateText) {
  tft.fillScreen(ST77XX_BLACK);
  centre("ROOT", 30, 3, ST77XX_WHITE);
  tft.drawFastHLine(60, 66, 120, 0x4208);
  centre(stateText, 80, 2, 0xCE79);
}

void drawLevel(int level) {
  static int prev[9] = {0};
  for (int i = 0; i < 9; i++) {
    int h = 6 + (level * (i + 3) / 11) % 40;
    int x = 24 + i * 22;
    tft.fillRect(x, 200 - prev[i], 14, prev[i], ST77XX_BLACK);
    tft.fillRect(x, 200 - h, 14, h, 0x07E0);
    prev[i] = h;
  }
}

void say(const String &who, const String &what) {
  tft.fillRect(0, 108, 240, 84, ST77XX_BLACK);
  centre(who, 110, 1, 0x8410);
  String a = what.substring(0, 22);
  String b = what.length() > 22 ? what.substring(22, 44) : "";
  centre(a, 126, 2, ST77XX_WHITE);
  if (b.length()) centre(b, 150, 2, ST77XX_WHITE);
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
  cfg.dma_buf_count = 12;
  cfg.dma_buf_len = 512;
  i2s_pin_config_t p = {};
  p.mck_io_num = I2S_PIN_NO_CHANGE; p.bck_io_num = SPK_BCLK;
  p.ws_io_num = SPK_LRCK; p.data_out_num = SPK_DIN; p.data_in_num = I2S_PIN_NO_CHANGE;
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &p);
  i2s_zero_dma_buffer(I2S_NUM_0);
}

void setupMic() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = SAMPLE_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 512;
  i2s_pin_config_t p = {};
  p.mck_io_num = I2S_PIN_NO_CHANGE; p.bck_io_num = MIC_SCK;
  p.ws_io_num = MIC_WS; p.data_out_num = I2S_PIN_NO_CHANGE; p.data_in_num = MIC_SD;
  i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_1, &p);
}

int sendMicBlock(uint8_t *dst, size_t cap, size_t *written) {
  *written = 0;
  static int16_t raw[MIC_BLOCK / 2];
  size_t read = 0;
  if (i2s_read(I2S_NUM_1, raw, MIC_BLOCK, &read, 5 / portTICK_PERIOD_MS) != ESP_OK) return 0;
  if (read < 4) return 0;
  int frames = read / 4;
  int32_t peakL = 0, peakR = 0;
  for (int i = 0; i < frames; i++) {
    int16_t l = raw[i * 2], r = raw[i * 2 + 1];
    if (abs(l) > peakL) peakL = abs(l);
    if (abs(r) > peakR) peakR = abs(r);
  }
  bool rightLive = peakR > peakL;
  size_t bytes = (size_t)frames * 2;
  if (bytes > cap) bytes = cap;
  bytes &= ~1u;
  int16_t *out = (int16_t *)dst;
  for (size_t i = 0; i * 2 < bytes; i++) out[i] = rightLive ? raw[i * 2 + 1] : raw[i * 2];
  *written = bytes;
  return peakL > peakR ? peakL : peakR;
}

// --------------------------------------------------------------- network ----

bool fetchJob(int *id) {
  HTTPClient http;
  String url = String("http://") + HOST + ":" + String(HTTP_PORT) + "/job";
  http.begin(url);
  http.setTimeout(4000);
  int code = http.GET();
  bool got = false;
  if (code == 200) {
    String body = http.getString();
    int pos = body.indexOf("\"id\":");
    if (pos >= 0) { *id = body.substring(pos + 5).toInt(); got = true; }
  }
  http.end();
  return got;
}

void fetchAudio(int id) {
  HTTPClient http;
  String url = String("http://") + HOST + ":" + String(HTTP_PORT) + "/a/" + String(id) + ".wav";
  http.begin(url);
  http.setTimeout(6000);
  int code = http.GET();
  if (code != 200) { Serial.printf("audio %d: HTTP %d\n", id, code); http.end(); return; }
  int total = http.getSize();
  WiFiClient *stream = http.getStreamPtr();
  size_t got = 0, skipped = 0;
  uint32_t started = millis();
  while (http.connected() && (got < (size_t)total || total <= 0)) {
    int avail = stream->available();
    if (avail <= 0) {
      if (millis() - started > 8000) break;
      delay(2);
      continue;
    }
    uint8_t tmp[512];
    int n = stream->readBytes(tmp, min((size_t)avail, sizeof(tmp)));
    if (n <= 0) break;
    for (int i = 0; i < n; i++) {
      if (skipped < 44) { skipped++; continue; }
      ringWrite(tmp[i]);
    }
    got += n;
    started = millis();
  }
  http.end();
  Serial.printf("audio %d: %u B fetched, ring %u\n", id, (unsigned)got, (unsigned)ringUsed());
}

bool postUtterance(uint8_t *pcm, size_t len) {
  HTTPClient http;
  String url = String("http://") + HOST + ":" + String(HTTP_PORT) + "/utt";
  http.begin(url);
  http.setTimeout(12000);
  http.addHeader("Content-Type", "application/octet-stream");
  int code = http.POST(pcm, len);
  http.end();
  Serial.printf("post %u bytes -> HTTP %d\n", (unsigned)len, code);
  return code > 0 && code < 300;
}

// ------------------------------------------------------------------ setup ----

#include <esp_task_wdt.h>
#define WDT_SECONDS 20

void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\n=== ROOT terminal v7 (http) ===");

  pinMode(BTN_PIN, INPUT_PULLUP);
  led.begin();
  led.setBrightness(60);
  setLed(0, 0, 120);

  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, HIGH);
  delay(50);
#ifdef PROBE_DISPLAY
  probeDisplay();
#else
  lcdSPI.begin(21, -1, 47, -1);
  tft.init(240, 240, SPI_MODE0);
  tft.setRotation(0);
#endif
  tft.setSPISpeed(40000000);
  tft.setTextWrap(false);
  drawChrome("hold to talk");

  ringBuf = (uint8_t *)ps_malloc(RING_BYTES);
  if (!ringBuf) ringBuf = (uint8_t *)malloc(RING_BYTES);

  setupSpeaker();
  setupMic();
  xTaskCreatePinnedToCore(playbackTask, "play", 4096, nullptr, 4, nullptr, 1);

  WiFiManager wm;
  wm.setConfigPortalTimeout(300);
  if (!wm.autoConnect("Root-ESP32-Setup")) { setLed(120, 0, 0); delay(3000); ESP.restart(); }
  Serial.printf("wifi: %s\n", WiFi.localIP().toString().c_str());

  ArduinoOTA.setHostname(OTA_HOST);
  ArduinoOTA.setPassword("root-vps-ota");
  ArduinoOTA.begin();

  esp_task_wdt_config_t wdt = {};
  wdt.timeout_ms = WDT_SECONDS * 1000;
  wdt.idle_core_mask = 0;
  wdt.trigger_panic = true;
  if (esp_task_wdt_init(&wdt) != ESP_OK) {
    esp_task_wdt_reconfigure(&wdt);      // already initialised by the core
  }
  esp_task_wdt_add(NULL);
  Serial.printf("watchdog: %d s\n", WDT_SECONDS);

  drawChrome("hold to talk");
}

// ------------------------------------------------------------------- loop ----

void loop() {
  ArduinoOTA.handle();

  static bool recording = false;
  static uint8_t *utter = nullptr;
  static size_t utterLen = 0, utterCap = 0;
  static unsigned long lastPoll = 0, lastBeat = 0;
  static int polls = 0;
  bool pressed = (digitalRead(BTN_PIN) == LOW);

  if (pressed && !recording) {
    recording = true;
    if (!utter) utter = (uint8_t *)ps_malloc(MAX_UTTER);
    utterLen = 0; utterCap = MAX_UTTER;
    setLed(0, 160, 0);
    drawChrome("listening");
    Serial.println("btn: down");
  }

  if (recording) {
    for (int i = 0; i < 6; i++) {
      size_t wrote = 0;
      int peak = sendMicBlock(utter + utterLen, utterCap - utterLen, &wrote);
      utterLen += wrote;                      // only what the mic actually gave us
      if (i == 5) drawLevel(peak / 60);
    }
  }

  if (!pressed && recording) {
    recording = false;
    setLed(180, 120, 0);
    drawChrome("thinking");
    Serial.printf("btn: up, %u bytes captured\n", (unsigned)utterLen);
    if (utterLen > 8000) {
      postUtterance(utter, utterLen);
      say("YOU SAID", "(sent to Root)");
      lastPoll = 0;                            // ask for the reply immediately
    } else {
      say("TRY AGAIN", "hold the button longer");
    }
    utterLen = 0;
    setLed(0, 0, 120);
  }

  if (!recording && millis() - lastPoll > 1200) {
    lastPoll = millis();
    int id = 0;
    if (fetchJob(&id)) {
      polls++;
      if (id > 0) {
        drawChrome("speaking");
        setLed(0, 140, 180);
        fetchAudio(id);
        say("ROOT", "speaking…");
        setLed(0, 0, 120);
      }
    }
  }

  if (Serial.available()) {
    int ch = Serial.read();
    if (ch == 't' || ch == 'T') {
      Serial.println("selftest: posting a synthetic utterance");
      static int16_t tone[32000];              // 2 s of 440 Hz
      for (int i = 0; i < 32000; i++) tone[i] = (int16_t)(sinf(2.0f * PI * 440 * i / 16000.0f) * 6000);
      postUtterance((uint8_t *)tone, sizeof(tone));
    }
  }

  esp_task_wdt_reset();

  if (millis() - lastBeat > 15000) {
    lastBeat = millis();
    Serial.printf("beat: up=%lus heap=%u ring=%u polls=%d\n",
                  (unsigned long)(millis() / 1000), (unsigned)ESP.getFreeHeap(),
                  (unsigned)ringUsed(), polls);
  }
  delay(2);
}
