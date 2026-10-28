// m5stick_f6_remote: comando de bolso do Zoom F6 numa M5Stack M5StickS3 (ESP32-S3-PICO-1-N8R8, ecra 135x240, 2 botoes).
//
// SO funciona com o S3 do F6 em modo "F6 standalone" (gopro_gw, selector f6solo): liga-se ao Wi-Fi F6-REMOTE-xxxx desse S3
// como cliente e fala a mesma API que a pagina do telemovel (http://192.168.4.1/):
//   GET  /api/f6/lite                estado curto (ligado, a gravar, tempo, pistas armadas, medidores), 4 vezes por segundo
//   POST /api/f6/press?note=24|22    REC / STOP (o PLAY nao existe aqui, de proposito)
//
// O nome e a palavra-passe do Wi-Fi NAO estao no codigo: vao numa particao propria ("cfg", 4 KB) que o firmware/gravar.py
// escreve ao gravar (--ssid e --psk). Sem ela o ecra diz como a escrever. Formato dos 128 bytes da particao:
//   0-3 "F6RC"  4 versao (1)  5 tam. ssid  6 tam. psk  7 tam. host  8-39 ssid  40-103 psk  104-119 host (omissao 192.168.4.1)
//   120-125 zeros  126-127 soma (16 bits, little-endian) dos bytes 0-125
//
// Botoes (a segurar contra acidentes):
//   B (lateral)  3 toques seguidos (cada um a menos de 1,5 s do anterior) desbloqueiam o botao A durante 10 s. Um toque em B
//                com o A desbloqueado volta a bloquear.
//   A (frente)   desbloqueado: segurar para gravar (REC 0,7 s) ou parar (STOP 0,45 s), conforme o estado REAL do F6 (o que o
//                F6 disse, nao o que a pagina pensa). Enviado o comando, volta a bloquear sozinho: um desbloqueio, um comando.
// O estado so e dado como feito quando o F6 o confirma (como a pagina do telemovel); sem resposta do S3 mostra
// "STATE UNKNOWN" a piscar em vez de adivinhar.
//
// Ecra (horizontal 240x135): estado grande (REC com o tempo, READY, NO TRACK, F6 NOT LINKED, NO WIFI, STATE UNKNOWN),
// cadeado e contagem, 6 pistas (+ saida L/R) com a pista armada a verde e o medidor de 12 segmentos com pico, e no rodape o
// Wi-Fi (barras e dBm, tempo do ultimo pedido) e a bateria da M5StickS3. A do F6 nao aparece: o FRC-8 nao a traz.
// Com o A TRANCADO, cada toque nele alterna a vista do meio: pistas e medidores / Wi-Fi (nome da rede, qualidade, canal, IP e se o S3
// responde). O estado (REC, READY ...) e o cadeado ficam sempre no topo, em qualquer vista.
// Para nao haver flicker, o ecra desenha-se num buffer e so se envia ao painel a faixa de linhas que mudou (nada, se nada mudou).
//
// Compilar: ver LEIA-ME.txt (M5Unified, M5GFX, M5PM1; placa esp32s3, particoes de partitions.csv desta pasta).
#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_partition.h>

static const char *VERSAO = "m5stick_f6_remote 6";

// ---- ajustes ----------------------------------------------------------------------------------------------------------
#define ROTATION 1            // 1 = horizontal; 3 = horizontal virado ao contrario
#define BRIGHTNESS 120        // 0 a 255
#define BEEPS 0               // 1 = bip ao confirmar (por omissao NAO: um bip pode ficar gravado nos microfones)
#define UNLOCK_TAPS 3
#define TAP_GAP_MS 1500       // tempo maximo entre toques seguidos de B
#define UNLOCK_MS 10000       // quanto tempo o A fica desbloqueado
#define HOLD_REC_MS 700
#define HOLD_STOP_MS 450
#define POLL_MS 250
#define STALE_MS 1500         // sem resposta ha mais do que isto: o estado mostrado ja nao e de fiar (decide os comandos)
#define DISPLAY_STALE_MS 3000 // so para o ecra: um pedido atrasado nao faz o estado saltar entre READY e NO LINK (excepto a gravar: STALE_MS)
#define CONFIRM_MS 3000       // o F6 tem este tempo para confirmar o REC / STOP
#define SEND_TIMEOUT_MS 3000
#define METER_MAX 12
#define NOTE_REC 0x24
#define NOTE_STOP 0x22

// ---- configuracao do Wi-Fi (particao "cfg") ----------------------------------------------------------------------------
struct Cfg { char ssid[33]; char psk[65]; char host[17]; };
static Cfg cfg;

static bool cfgLoad() {
  const esp_partition_t *p = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, (esp_partition_subtype_t)0x40, "cfg");
  if (!p) return false;
  uint8_t b[128];
  if (esp_partition_read(p, 0, b, sizeof b) != ESP_OK) return false;
  if (b[0] != 'F' || b[1] != '6' || b[2] != 'R' || b[3] != 'C' || b[4] != 1) return false;
  uint16_t sum = 0;
  for (int i = 0; i < 126; i++) sum += b[i];
  if (sum != (uint16_t)(b[126] | (b[127] << 8))) return false;
  uint8_t ls = b[5], lp = b[6], lh = b[7];
  if (ls == 0 || ls > 32 || lp > 64 || lh > 16) return false;
  memcpy(cfg.ssid, b + 8, ls); cfg.ssid[ls] = 0;
  memcpy(cfg.psk, b + 40, lp); cfg.psk[lp] = 0;
  if (lh) { memcpy(cfg.host, b + 104, lh); cfg.host[lh] = 0; }
  else strcpy(cfg.host, "192.168.4.1");
  return true;
}

// ---- estado do F6 (escrito pela tarefa de pedidos, lido pelo ecra) -----------------------------------------------------------
struct F6State {
  uint32_t okMs;            // quando chegou o ultimo /api/f6/lite bom (0 = nunca)
  int connected;            // F6 ligado ao S3 por USB: 1/0
  int recording;            // 1/0, -1 = o F6 ainda nao disse
  long recMs;               // tempo de gravacao (do S3), -1 = n/a
  int armedKnown;
  uint8_t armed;            // bit n = pista n+1 armada
  uint8_t meters[10];       // 0..12; 8 e 9 = saida L/R
  uint16_t rtt;             // ms do ultimo pedido
};
static F6State st;
static SemaphoreHandle_t mtx;

enum CmdPhase { C_IDLE, C_SEND, C_WAIT, C_FAIL, C_UNKNOWN };
static volatile CmdPhase cmdPhase = C_IDLE;
static volatile uint8_t cmdNote = 0;
static volatile int cmdWant = 0;          // 1 = REC, 0 = STOP
static volatile uint32_t cmdMs = 0;
static char cmdMsg[48] = "";
static volatile bool wasRec = false;      // o ultimo estado bom dizia que gravava (para o aviso de ligacao perdida)

static int jBool(const String &s, const char *k) {
  int i = s.indexOf(String("\"") + k + "\":");
  if (i < 0) return -1;
  i += strlen(k) + 3;
  if (s.startsWith("true", i)) return 1;
  if (s.startsWith("false", i)) return 0;
  return -1;
}
static long jNum(const String &s, const char *k, long def) {
  int i = s.indexOf(String("\"") + k + "\":");
  if (i < 0) return def;
  i += strlen(k) + 3;
  if (!(isdigit(s[i]) || s[i] == '-')) return def;
  return s.substring(i).toInt();
}
// "chave":[a,b,c] -> out[0..n-1]; devolve quantos leu, ou -1 se for null / nao existir
static int jArr(const String &s, const char *k, int *out, int max) {
  int i = s.indexOf(String("\"") + k + "\":");
  if (i < 0) return -1;
  i += strlen(k) + 3;
  if (s[i] != '[') return -1;
  i++;
  int n = 0;
  while (n < max && i < (int)s.length() && s[i] != ']') {
    out[n++] = s.substring(i).toInt();
    int c = s.indexOf(',', i), e = s.indexOf(']', i);
    if (e < 0) break;
    if (c < 0 || c > e) break;
    i = c + 1;
  }
  return n;
}

static int httpReq(bool post, const String &path, String &out, int to) {
  HTTPClient http;
  http.setConnectTimeout(to);
  http.setTimeout(to);
  if (!http.begin(String("http://") + cfg.host + path)) return -1;
  int code = post ? http.POST("") : http.GET();
  if (code > 0) out = http.getString();
  http.end();
  return code;
}

static void pollOnce() {
  String body;
  uint32_t t0 = millis();
  int code = httpReq(false, "/api/f6/lite", body, 1200);
  if (code != 200 || body.indexOf("\"ok\":true") < 0) return;
  F6State n;
  memset(&n, 0, sizeof n);
  n.okMs = millis();
  n.rtt = (uint16_t)min<uint32_t>(9999, millis() - t0);
  n.connected = jBool(body, "connected");
  n.recording = jBool(body, "recording");
  n.recMs = jNum(body, "rec_ms", -1);
  int arr[10];
  int na = jArr(body, "armed", arr, 8);
  n.armedKnown = na >= 0;
  for (int i = 0; i < na; i++) if (arr[i] >= 1 && arr[i] <= 8) n.armed |= 1 << (arr[i] - 1);
  int nm = jArr(body, "meters", arr, 10);
  for (int i = 0; i < nm && i < 10; i++) n.meters[i] = (uint8_t)constrain(arr[i], 0, METER_MAX);
  xSemaphoreTake(mtx, portMAX_DELAY);
  st = n;
  wasRec = n.recording == 1;
  xSemaphoreGive(mtx);
}

static void sendCmd() {
  char path[40];
  snprintf(path, sizeof path, "/api/f6/press?note=%02x", cmdNote);
  String body;
  int code = httpReq(true, path, body, SEND_TIMEOUT_MS);
  if (code == 200 && body.indexOf("\"ok\":true") >= 0) { cmdMs = millis(); cmdPhase = C_WAIT; return; }
  if (code <= 0) { cmdPhase = C_UNKNOWN; cmdMs = millis(); return; }      // o pedido pode ter chegado: nao se sabe
  int i = body.indexOf("\"error\":\"");                                    // recusado pelo S3: diz porque
  if (i >= 0) {
    i += 9;
    int e = body.indexOf('"', i);
    String m = body.substring(i, e < 0 ? body.length() : e);
    snprintf(cmdMsg, sizeof cmdMsg, "%s", m.c_str());
  } else snprintf(cmdMsg, sizeof cmdMsg, "HTTP %d", code);
  cmdMs = millis();
  cmdPhase = C_FAIL;
}

static void netTask(void *) {
  uint32_t lastBegin = 0;
  for (;;) {
    uint32_t t0 = millis();
    if (WiFi.status() == WL_CONNECTED) {
      if (cmdPhase == C_SEND) sendCmd();
      pollOnce();
    } else if (t0 - lastBegin > 15000) {                 // sem Wi-Fi: volta a tentar de 15 em 15 s
      lastBegin = t0;
      WiFi.disconnect();
      WiFi.begin(cfg.ssid, cfg.psk);
    }
    uint32_t dt = millis() - t0;
    vTaskDelay(pdMS_TO_TICKS(dt >= POLL_MS ? 20 : POLL_MS - dt));
  }
}

// ---- ecra ----------------------------------------------------------------------------------------------------------------
static M5Canvas cv(&M5.Display);
static uint8_t peaks[10];
static uint32_t peakT[10];
static uint32_t batMs = 0, batSampMs = 0, batStepMs = 0;
static int batPct = -1, batChg = 0;

// Bateria: o M5Unified calcula a % so pela tensao instantanea (3300..4150 mV, ~8,5 mV por 1 %), por isso o Wi-Fi a
// transmitir e o backlight fazem a tensao cair dezenas de mV e a % oscila 10 % ou mais. Filtro: mediana das ultimas
// ~20 s de tensao e, ao descarregar, a % mostrada so desce (a carregar so sobe), um passo de cada vez.
// Nos primeiros ~20 s (a janela ainda a encher) a % segue a mediana nos dois sentidos: a descida so vale depois, senao uma leitura
// baixa do arranque (o Wi-Fi a ligar) ficava como piso para sempre.
#define BAT_N 40
static int16_t batRing[BAT_N];
static uint8_t batCnt = 0, batIdx = 0;
static int batChgPend = 0;
static bool batChgInit = false;
static uint32_t batChgSince = 0;

static void batUpdate(uint32_t now) {
  int chg = M5.Power.isCharging() == m5::Power_Class::is_charging;
  if (!batChgInit) { batChgInit = true; batChg = batChgPend = chg; }
  else if (chg == batChg) batChgPend = chg;
  else if (chg != batChgPend) { batChgPend = chg; batChgSince = now; }
  else if (now - batChgSince >= 3000) {                              // ligar/desligar o USB muda a tensao de repente; so conta se durar 3 s
    batChg = chg; batCnt = 0; batIdx = 0; batPct = -1;               // (perto dos 100 % o carregador pode ligar e desligar: nao pisca "--")
  }
  if (now - batSampMs >= 500) {
    batSampMs = now;
    int16_t mv = M5.Power.getBatteryVoltage();
    if (mv > 0) { batRing[batIdx] = mv; batIdx = (batIdx + 1) % BAT_N; if (batCnt < BAT_N) batCnt++; }
  }
  if (batCnt < 3 || now - batMs < 2000) return;
  batMs = now;
  int16_t tmp[BAT_N];
  memcpy(tmp, batRing, sizeof tmp);
  for (int i = 1; i < batCnt; i++) { int16_t v = tmp[i]; int j = i - 1; while (j >= 0 && tmp[j] > v) { tmp[j + 1] = tmp[j]; j--; } tmp[j + 1] = v; }
  int mv = tmp[batCnt / 2];
  int pct = constrain((mv - 3300) * 100 / (4150 - 3350), 0, 100);
  if (batPct < 0 || batCnt < BAT_N) { batPct = pct; batStepMs = now; return; }
  // passo de 1 % a cada 10 s; 1 % por ciclo se a distancia for grande (>=10)
  bool far = abs(pct - batPct) >= 10;
  if (now - batStepMs < (far ? 2000UL : 10000UL)) return;
  if (!batChg && pct < batPct) { batPct--; batStepMs = now; }
  else if (batChg && pct > batPct) { batPct++; batStepMs = now; }
}

static uint16_t C(uint8_t r, uint8_t g, uint8_t b) { return cv.color565(r, g, b); }

// Estado de desbloqueio e dos toques
static uint32_t unlockUntil = 0, lastTap = 0, holdT0 = 0;
static int taps = 0;
static int holdAct = -1;                  // 1 REC, 0 STOP, -1 nada a segurar
static char flashMsg[40] = "";
static uint32_t flashUntil = 0;
static uint16_t flashCol = 0;

static void beep(int f) {
#if BEEPS
  M5.Speaker.tone(f, 90);
#else
  (void)f;
#endif
}
static void flash(const char *m, uint16_t col, uint32_t ms) {
  snprintf(flashMsg, sizeof flashMsg, "%s", m);
  flashCol = col;
  flashUntil = millis() + ms;
}

static void drawWifi(int x, int y, int rssi, bool on) {
  int bars = !on ? 0 : (rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : rssi > -85 ? 1 : 0);
  for (int i = 0; i < 4; i++) {
    int h = 3 + i * 3;
    cv.fillRect(x + i * 5, y + 12 - h, 4, h, i < bars ? (bars <= 1 ? C(255, 60, 60) : bars == 2 ? C(255, 215, 0) : C(60, 210, 90)) : C(70, 70, 70));
  }
}

static void drawLock(int x, int y, bool open, uint16_t col) {
  cv.fillRoundRect(x, y + 9, 18, 13, 2, col);
  if (open) { cv.drawArc(x + 13, y + 9, 8, 6, 180, 360, col); cv.drawFastVLine(x + 5, y + 9, 5, col); }
  else cv.drawArc(x + 9, y + 9, 7, 5, 180, 360, col);
}

static uint8_t viewMode = 0;              // 0 pistas e medidores (pagina 1/3), 1 Wi-Fi (2/3), 2 baterias e S3 (3/3)
static uint8_t *prevFrame = nullptr;      // o ultimo quadro enviado ao painel (para enviar so o que mudou)

// Primeira e ultima linha que diferem entre dois quadros (rowB bytes por linha). false = iguais.
static bool dirtyBand(const uint8_t *cur, const uint8_t *prev, int h, size_t rowB, int *y0, int *y1) {
  *y0 = *y1 = -1;
  for (int y = 0; y < h; y++)
    if (memcmp(cur + y * rowB, prev + y * rowB, rowB)) { if (*y0 < 0) *y0 = y; *y1 = y; }
  return *y0 >= 0;
}

// Envia o buffer ao painel: so as linhas que mudaram (o ST7789 nao tem sinal de sincronismo: reescrever o ecra todo a cada volta cintila).
static void present() {
  int w = cv.width(), h = cv.height(), y0 = 0, y1 = h - 1;
  size_t rowB = (size_t)w * 2;
  const uint8_t *cur = (const uint8_t *)cv.getBuffer();
  if (prevFrame && cur) {
    if (!dirtyBand(cur, prevFrame, h, rowB, &y0, &y1)) return;       // nada mudou: nem se mexe no painel
    memcpy(prevFrame + (size_t)y0 * rowB, cur + (size_t)y0 * rowB, (size_t)(y1 - y0 + 1) * rowB);
  }
  M5.Display.setClipRect(0, y0, w, y1 - y0 + 1);
  cv.pushSprite(0, 0);
  M5.Display.clearClipRect();
}

// Texto na letra grande (Font4) se couber em maxW; senao na pequena (Font2, ainda se le), cortado ao que cabe. Nunca ha letra micro (Font0).
static void drawFit(const char *txt, int x, int y, int maxW, uint16_t col) {
  cv.setTextColor(col, C(0, 0, 0));
  cv.setTextDatum(top_left);
  cv.setFont(&fonts::Font4);
  if (cv.textWidth(txt) <= maxW) { cv.drawString(txt, x, y); return; }
  cv.setFont(&fonts::Font2);
  char t[40];
  snprintf(t, sizeof t, "%s", txt);
  while (strlen(t) > 3 && cv.textWidth(t) > maxW) t[strlen(t) - 1] = 0;
  cv.drawString(t, x, y + 5);
}

// Pagina 2/3: o Wi-Fi (nome da rede, qualidade e dBm, canal e IP), em letra grande. (Sem tipos proprios nos parametros: o arduino-cli poe o
// prototipo antes deles.)
static void pageWifi(bool wifiOk, int rssi) {
  const uint16_t BLK = C(0, 0, 0), WHT = C(255, 255, 255), LG = C(170, 170, 170), GRN = C(40, 200, 70), YEL = C(255, 215, 0), RED = C(230, 30, 30);
  int bars = !wifiOk ? 0 : (rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : rssi > -85 ? 1 : 0);
  uint16_t qc = bars >= 3 ? GRN : bars == 2 ? YEL : RED;
  const char *q = !wifiOk ? "" : bars == 4 ? "excellent" : bars == 3 ? "good" : bars == 2 ? "fair" : "weak";
  char t[48];
  drawFit(cfg.ssid, 6, 52, 228, WHT);                                 // o nome da rede (a largura toda: F6-REMOTE-xxxx mede ~210 px)
  if (wifiOk) snprintf(t, sizeof t, "%d dBm", rssi);
  else {
    wl_status_t w = WiFi.status();
    snprintf(t, sizeof t, "%s", w == WL_NO_SSID_AVAIL ? "not found" : w == WL_CONNECT_FAILED ? "wrong password" : "searching");
  }
  drawFit(t, 6, 82, 158, wifiOk ? qc : YEL);                          // os dBm, ou porque nao liga
  for (int i = 0; i < 4; i++) {                                       // barras, a direita da linha dos dBm
    int h = 8 + i * 6;
    cv.fillRect(170 + i * 18, 108 - h, 12, h, i < bars ? qc : C(60, 60, 60));
  }
  cv.setFont(&fonts::Font2);
  cv.setTextDatum(top_left);
  cv.setTextColor(LG, BLK);
  if (wifiOk) snprintf(t, sizeof t, "%s   ch %d   %s", q, (int)WiFi.channel(), WiFi.localIP().toString().c_str());
  else snprintf(t, sizeof t, "S3 at %s", cfg.host);
  while (strlen(t) > 3 && cv.textWidth(t) > 228) t[strlen(t) - 1] = 0;
  cv.drawString(t, 6, 114);
}

// Pagina 3/3: a bateria da M5StickS3 e se o S3 responde, em letra grande.
static void pageSystem(int m5pct, bool m5chg, uint32_t okMs, uint16_t rtt, bool wifiOk, uint32_t now) {
  const uint16_t BLK = C(0, 0, 0), LG = C(170, 170, 170), GRN = C(40, 200, 70), YEL = C(255, 215, 0), RED = C(230, 30, 30);
  char t[48];
  if (m5pct < 0) snprintf(t, sizeof t, "M5   --");
  else snprintf(t, sizeof t, "M5   %d%%", m5pct);
  drawFit(t, 6, 54, 150, m5pct < 0 ? LG : (m5pct <= 15 ? RED : m5pct <= 30 ? YEL : GRN));
  if (m5chg) {                                                        // a carregar: por extenso, ao lado
    cv.setFont(&fonts::Font2);
    cv.setTextDatum(top_left);
    cv.setTextColor(GRN, BLK);
    cv.drawString("charging", 160, 59);
  }
  bool s3ok = okMs && (now - okMs) <= DISPLAY_STALE_MS;
  uint16_t col = LG;
  if (s3ok) { snprintf(t, sizeof t, "S3 answers  %u ms", (unsigned)rtt); col = GRN; }
  else if (wifiOk) { snprintf(t, sizeof t, okMs ? "S3 silent for %lu s" : "S3 does not answer", (unsigned long)((now - okMs) / 1000)); col = YEL; }
  else snprintf(t, sizeof t, "S3 -");
  drawFit(t, 6, 88, 228, col);                                        // o S3, na letra grande
}

static void draw() {
  xSemaphoreTake(mtx, portMAX_DELAY);
  F6State s = st;
  xSemaphoreGive(mtx);
  uint32_t now = millis();
  bool wifiOk = WiFi.status() == WL_CONNECTED;
  bool linked = s.okMs && (now - s.okMs) <= (wasRec ? STALE_MS : DISPLAY_STALE_MS);
  bool blink = (now / 400) & 1;
  const uint16_t RED = C(230, 30, 30), GRN = C(40, 200, 70), YEL = C(255, 215, 0), GRY = C(70, 70, 70), LG = C(170, 170, 170), WHT = C(255, 255, 255);

  // --- o texto grande e a cor ---
  char big[28]; char hint[44] = ""; uint16_t bg = GRY, fg = WHT;
  if (!wifiOk) { strcpy(big, "NO WIFI"); bg = YEL; fg = C(0, 0, 0); if (viewMode == 0) snprintf(hint, sizeof hint, "%.30s", cfg.ssid); }
  else if (!linked) {
    if (cmdPhase == C_UNKNOWN || cmdPhase == C_WAIT || wasRec) {
      strcpy(big, "STATE UNKNOWN"); bg = blink ? YEL : C(60, 50, 0); fg = blink ? C(0, 0, 0) : YEL;
      snprintf(hint, sizeof hint, "%s", wasRec ? "was REC: LOOK AT THE F6" : "LOOK AT THE F6");
    } else { strcpy(big, "NO LINK TO S3"); bg = YEL; fg = C(0, 0, 0); }
  }
  else if (cmdPhase == C_UNKNOWN) {
    strcpy(big, "STATE UNKNOWN"); bg = blink ? YEL : C(60, 50, 0); fg = blink ? C(0, 0, 0) : YEL;
    strcpy(hint, "no answer to the command");
  }
  else if (s.connected != 1) { strcpy(big, "F6 NOT LINKED"); bg = GRY; }
  else if (s.recording == 1) {
    long sec = s.recMs >= 0 ? s.recMs / 1000 : 0;
    snprintf(big, sizeof big, "REC %02ld:%02ld", sec / 60, sec % 60); bg = RED;
  }
  else if (s.armedKnown && s.armed == 0) { strcpy(big, "NO TRACK"); bg = YEL; fg = C(0, 0, 0); }
  else { strcpy(big, s.recording == 0 ? "READY" : "F6 LINKED"); bg = C(20, 120, 50); }
  if (cmdPhase == C_WAIT) snprintf(hint, sizeof hint, "sent, waiting for the F6 ...");
  else if (cmdPhase == C_FAIL) snprintf(hint, sizeof hint, "%.40s", cmdMsg);
  else if (now < flashUntil) snprintf(hint, sizeof hint, "%s", flashMsg);
  bool unlocked = now < unlockUntil;
  if (!hint[0]) {
    if (unlocked) strcpy(hint, s.recording == 1 ? "HOLD A: STOP" : "HOLD A: REC");
    else snprintf(hint, sizeof hint, "B x3 unlock    A: page %d/3", viewMode + 1);
  }

  cv.fillSprite(C(0, 0, 0));
  cv.fillRect(0, 0, 240, 32, bg);
  cv.setTextColor(fg, bg);
  cv.setFont(&fonts::Font4);
  cv.setTextDatum(middle_left);
  cv.drawString(big, 6, 16);
  // cadeado e toques
  uint16_t lc = unlocked ? C(40, 255, 100) : (bg == YEL ? C(0, 0, 0) : WHT);
  drawLock(221, 3, unlocked, lc);                                  // encostado a direita: "STATE UNKNOWN" mede ~215 px
  if (!unlocked && taps > 0 && cv.textWidth(big) < 150)       // so depois do primeiro toque, e nunca por cima do texto grande
    for (int i = 0; i < UNLOCK_TAPS; i++) cv.fillCircle(170 + i * 10, 16, 3, i < taps ? lc : (bg == YEL ? C(120, 100, 0) : C(110, 110, 110)));
  // contagem do desbloqueio
  if (unlocked) cv.fillRect(0, 32, (int)(240UL * (unlockUntil - now) / UNLOCK_MS), 3, C(40, 255, 100));
  // linha de ajuda / mensagem
  cv.setFont(&fonts::Font2);
  cv.setTextDatum(top_left);
  uint16_t hc = cmdPhase == C_FAIL ? RED : (now < flashUntil ? flashCol : LG);
  cv.setTextColor(hc, C(0, 0, 0));
  cv.drawString(hint, 6, 37);
  // progresso do botao A (segurar)
  if (holdAct >= 0) {
    uint32_t need = holdAct ? HOLD_REC_MS : HOLD_STOP_MS, held = now - holdT0;
    cv.fillRect(0, 54, (int)min<uint32_t>(240, 240UL * held / need), 5, holdAct ? RED : WHT);
  }

  int rssi = wifiOk ? WiFi.RSSI() : -100;
  batUpdate(now);
  if (viewMode == 1) pageWifi(wifiOk, rssi);
  else if (viewMode == 2) pageSystem(batPct, batChg, s.okMs, s.rtt, wifiOk, now);
  else {
    // --- pagina 1/3: pistas 1-6 e saida L/R (indices 8 e 9) ---
    const int idx[8] = {0, 1, 2, 3, 4, 5, 8, 9};
    const char *nm[8] = {"1", "2", "3", "4", "5", "6", "L", "R"};
    for (int c = 0; c < 8; c++) {
      int x = 10 + c * 28, i = idx[c];
      bool armed = c < 6 && s.armedKnown && (s.armed & (1 << i));
      bool live = linked && s.connected == 1;
      int v = live ? s.meters[i] : 0;
      if (v >= peaks[i] || now - peakT[i] > 1200) { if (v >= peaks[i]) peaks[i] = v; else peaks[i] = max(v, peaks[i] - 1); peakT[i] = now - (v >= peaks[i] ? 0 : 900); }
      for (int sg = 0; sg < METER_MAX; sg++) {
        int y = 56 + (METER_MAX - 1 - sg) * 4;
        uint16_t col = sg < v ? (sg == METER_MAX - 1 ? RED : sg >= 9 ? YEL : (armed || c >= 6 ? GRN : C(30, 110, 50))) : C(38, 38, 38);
        if (sg == peaks[i] - 1 && peaks[i] > v && live) col = WHT;
        cv.fillRect(x, y, 20, 3, col);
      }
      uint16_t lab = armed ? GRN : C(45, 45, 45);
      cv.fillRoundRect(x, 105, 20, 14, 2, lab);
      cv.setFont(&fonts::Font2);
      cv.setTextColor(armed ? C(0, 0, 0) : LG, lab);
      cv.setTextDatum(middle_center);
      cv.drawString(nm[c], x + 10, 112);
    }
    // --- rodape (letra pequena mas legivel, Font2): Wi-Fi e bateria da M5; o detalhe esta nas paginas 2 e 3 ---
    drawWifi(4, 122, rssi, wifiOk);
    cv.setFont(&fonts::Font2);
    cv.setTextDatum(middle_left);
    char tb[24];
    cv.setTextColor(LG, C(0, 0, 0));
    if (wifiOk) snprintf(tb, sizeof tb, "%d", rssi); else snprintf(tb, sizeof tb, "no wifi");
    cv.drawString(tb, 28, 128);
    uint16_t bc = batPct < 0 ? LG : (batPct <= 15 ? RED : batPct <= 30 ? YEL : GRN);
    if (batPct < 0) snprintf(tb, sizeof tb, "M5 --"); else snprintf(tb, sizeof tb, "M5 %s%d%%", batChg ? "+" : "", batPct);
    cv.setTextColor(bc, C(0, 0, 0));
    cv.drawString(tb, 90, 128);
  }
  present();
}

static void screenMsg(const char *l1, const char *l2 = "", const char *l3 = "") {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextDatum(top_left);
  M5.Display.setFont(&fonts::Font2);
  M5.Display.setTextColor(TFT_YELLOW, TFT_BLACK);
  M5.Display.drawString(l1, 4, 6);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.drawString(l2, 4, 34);
  M5.Display.drawString(l3, 4, 58);
}

// ---- botoes ---------------------------------------------------------------------------------------------------------------
static void buttons() {
  uint32_t now = millis();
  bool unlocked = now < unlockUntil;
  if (!unlocked) {
    unlockUntil = 0;
    holdAct = -1;
  }
  // B: 3 toques seguidos desbloqueiam; com o A desbloqueado, um toque volta a bloquear
  if (M5.BtnB.wasPressed()) {
    if (unlocked) { unlockUntil = 0; taps = 0; holdAct = -1; flash("locked", C(170, 170, 170), 1200); }
    else {
      if (now - lastTap > TAP_GAP_MS) taps = 0;
      taps++;
      lastTap = now;
      if (taps >= UNLOCK_TAPS) { taps = 0; unlockUntil = now + UNLOCK_MS; beep(1800); }
    }
  }
  if (!unlocked && taps && now - lastTap > TAP_GAP_MS) taps = 0;      // toques a mais de 1,5 s uns dos outros nao contam
  unlocked = now < unlockUntil;
  if (!unlocked) {
    if (M5.BtnA.wasPressed()) {                     // A trancado: cada toque alterna a vista (pistas / Wi-Fi); nao envia nada
      viewMode = (viewMode + 1) % 3;
      memset(peaks, 0, sizeof peaks);
    }
    return;
  }

  // A: segurar para enviar
  if (M5.BtnA.wasPressed()) {
    xSemaphoreTake(mtx, portMAX_DELAY);
    F6State s = st;
    xSemaphoreGive(mtx);
    bool fresh = s.okMs && (now - s.okMs) <= STALE_MS;
    if (!fresh || s.connected != 1 || s.recording < 0 || cmdPhase == C_SEND || cmdPhase == C_WAIT || cmdPhase == C_UNKNOWN) {
      flash(fresh ? (s.connected == 1 ? "state not known yet" : "F6 not linked") : "STATE UNKNOWN: look at F6", C(255, 215, 0), 2500);
    } else if (s.recording == 0 && s.armedKnown && s.armed == 0) {
      flash("no track", C(255, 215, 0), 2500);
    } else {
      holdAct = s.recording == 1 ? 0 : 1;           // grava ou para, conforme o estado REAL do F6
      holdT0 = now;
    }
  }
  if (holdAct >= 0) {
    if (!M5.BtnA.isPressed()) { holdAct = -1; }     // largou antes do tempo: nada se envia
    else if (now - holdT0 >= (uint32_t)(holdAct ? HOLD_REC_MS : HOLD_STOP_MS)) {
      xSemaphoreTake(mtx, portMAX_DELAY);
      int rec = st.recording;
      xSemaphoreGive(mtx);
      if ((holdAct == 1 && rec == 0) || (holdAct == 0 && rec == 1)) {      // o estado nao mudou enquanto se segurava
        cmdWant = holdAct;
        cmdNote = holdAct ? NOTE_REC : NOTE_STOP;
        cmdMsg[0] = 0;
        cmdPhase = C_SEND;
        beep(2400);
      } else flash("state changed, not sent", C(255, 215, 0), 2500);
      holdAct = -1;
      unlockUntil = 0;                              // um desbloqueio, um comando
    }
  }
}

static void cmdProgress() {
  uint32_t now = millis();
  xSemaphoreTake(mtx, portMAX_DELAY);
  F6State s = st;
  xSemaphoreGive(mtx);
  bool fresh = s.okMs && (now - s.okMs) <= STALE_MS;
  if (cmdPhase == C_WAIT) {
    if (fresh && s.okMs > cmdMs && ((cmdWant == 1 && s.recording == 1) || (cmdWant == 0 && s.recording == 0))) {
      cmdPhase = C_IDLE;
      flash(cmdWant ? "REC OK" : "STOP OK", C(40, 200, 70), 1500);
    } else if (now - cmdMs > CONFIRM_MS) {
      if (fresh) {                                  // o S3 responde e o estado nao mudou: o F6 nao confirmou
        snprintf(cmdMsg, sizeof cmdMsg, "F6 did NOT confirm %s", cmdWant ? "REC" : "STOP");
        cmdMs = now;
        cmdPhase = C_FAIL;
      } else {                                      // sem ligacao ao S3: o comando pode ter sido executado, nao se sabe
        cmdMs = now;
        cmdPhase = C_UNKNOWN;
      }
    }
  } else if (cmdPhase == C_FAIL) {
    if (now - cmdMs > 6000) cmdPhase = C_IDLE;
  } else if (cmdPhase == C_UNKNOWN) {
    if (fresh && s.okMs > cmdMs) {                  // a ligacao voltou: o estado verdadeiro substitui o "nao sei"
      cmdPhase = C_IDLE;
      flash(s.recording == 1 ? "link back: F6 IS RECORDING" : "link back: F6 is not recording", C(255, 215, 0), 6000);
    }
  }
}

void setup() {
  auto mc = M5.config();
  M5.begin(mc);
  Serial.setTxTimeoutMs(0);
  M5.Display.setRotation(ROTATION);
  M5.Display.setBrightness(BRIGHTNESS);
  M5.Speaker.setVolume(96);
  mtx = xSemaphoreCreateMutex();
  Serial.printf("%s\n", VERSAO);
  if (!cfgLoad()) {
    screenMsg("NO WIFI CONFIG", "flash with the SSID and password:", "gravar.py COMx --m5stick --ssid .. --psk ..");
    Serial.println("no cfg partition data: flash it with gravar.py --m5stick --ssid X --psk Y");
    for (;;) { M5.update(); delay(200); }
  }
  screenMsg(VERSAO, cfg.ssid, cfg.host);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(WIFI_PS_MIN_MODEM);
  WiFi.begin(cfg.ssid, cfg.psk);
  cv.setColorDepth(16);
  cv.createSprite(M5.Display.width(), M5.Display.height());
  prevFrame = (uint8_t *)malloc((size_t)cv.width() * cv.height() * 2);
  if (prevFrame) memset(prevFrame, 0xA5, (size_t)cv.width() * cv.height() * 2);       // o primeiro quadro envia-se todo
  xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);
}

void loop() {
  M5.update();
  buttons();
  cmdProgress();
  static uint32_t lastDraw = 0;
  if (millis() - lastDraw >= 50) { lastDraw = millis(); draw(); }
  delay(5);
}
