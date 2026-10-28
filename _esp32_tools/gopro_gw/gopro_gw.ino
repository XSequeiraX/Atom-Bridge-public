// Porta de ligacao GoPro (HERO8): o ESP32-S3 fala BLE (Open GoPro) com ate 3 GoPro e o bridge
// fala com ele por HTTP, na rede Wi-Fi das cameras. O S3 fica perto das GoPro e o bridge onde
// quiser: o alcance do BLE deixa de ser problema do Atom.
//
// Wi-Fi: sem segredos compilados. As redes (ate 2) ficam na memoria do gateway. Ao arrancar tenta-as por ordem
// (15 s cada); se nao houver nenhuma, ou nenhuma ligar, abre o AP aberto "GOPRO-S3-XXXX" com o portal em
// http://192.168.4.1 (lista as redes, escolhe-se uma e escreve-se a palavra-passe), como o gateway C3. O AP fica 3 min
// (mais enquanto alguem estiver ligado a ele); havendo redes guardadas, volta a tentar-as. O portal tambem esta em
// http://<ip>/ com o gateway ligado. Um gateway com o firmware anterior (Wi-Fi compilado) guarda-o no arranque novo.
//   GET /api/wifi  GET /api/wifi/scan  POST /api/wifi/save?s1=..&p1=..[&s2=..&p2=..]  POST /api/wifi/forget
// A configuracao inicial faz-se pelo Atom, por USB: o comando serie "WIFISET ssid|psk" guarda a rede (o Atom envia-o).
// (A password nao se corta: espacos nas pontas ficam. "WIFISETX hexssid|hexpsk" e o mesmo em hex, para nomes com espacos nas pontas.)
// HTTP porta 80 (argumentos na query ou no corpo "form"), respostas em JSON:
//   GET  /api/status                    o estado de cada GoPro configurada
//   POST /api/scan?secs=8               arranca uma procura de GoPro (BLE)
//   GET  /api/scan                      resultado da procura (done=true quando acabou)
//   POST /api/add?mac=..&type=1&name=.. configura uma GoPro (fica guardada na flash)
//   POST /api/remove?id=gp1             retira-a (esquece-se do bond)
//   POST /api/rec?id=gp1&on=1           grava / pára, com confirmacao pelo estado da camara
//   POST /api/clear_bonds               apaga todos os emparelhamentos
//   POST /api/fw?md5=..                 firmware novo (multipart, campo "fw"; o .bin do Arduino): grava na outra
//                                       particao OTA, confere o MD5 e reinicia. Recusado com uma GoPro a gravar.
//   POST /api/fw/rollback               volta ao firmware anterior (a outra particao) e reinicia
// UDP 5170 broadcast, de 5 em 5 s: "GPGW ip=<ip> cams=<n>" (o bridge descobre o endereco).
//
// Serie USB (diagnostico): STATUS | SCAN [s] | ADD mac type name | DEL id | REC id 0|1 | CLEAR.
//
// Token (2026-10-14): o Atom grava-o pela USB na configuracao (comando serie "TOKENSET <hex>"; so pela USB, nunca pela
// rede). Com ele guardado, o firmware, o rollback, os resets, os emparelhamentos, add/remove, o REC e o Wi-Fi (fora do
// modo de configuracao) exigem o cabecalho X-GW-Token igual, e o reinicio por UDP e "GPGW-RESET <token>". Sem token
// aceita tudo, como antes. O /api/status diz "token": true|false.
//
// LED RGB (WS2812, LED_PIN): azul ao arrancar; verde com GoPro ligadas; amarelo a piscar sem GoPro ligada ou
// se alguma que ja tinha estado ligada caiu; magenta com o AP de configuracao aberto. O estado vai em "led" no
// /api/status (ok | miss | boot | ap).
// Modo F6 (selector, 2026-10-15): o mesmo S3 faz de comando FRC-8 do Zoom F6, pela USB (ver "modo F6" mais abaixo).
//   GET  /api/f6                        o estado do F6 (tambem vai em "f6" no /api/status, com "mode")
//   POST /api/f6/press?note=24          carrega e larga um botao do FRC-8 (REC 24, STOP 22, PLAY 23, USER 10-12...)
//   POST /api/f6/midi?hex=f0+52+...     uma mensagem MIDI ao F6 (teclado e roda do menu do FRC-8)
//   POST /api/mode?m=gopro|f6|f6solo    muda de modo e reinicia (recusado a gravar). Serie: MODE gopro|f6
//   Modo F6 AUTONOMO (f6solo, 2026-10-16): sem o Atom. O S3 abre o seu Wi-Fi "F6-REMOTE-XXXX" (WPA2, a palavra-passe vem
//   com o pedido: &psk=, 8 a 63 caracteres), serve a pagina do comando em http://192.168.4.1/ (REC/STOP/PLAY e os
//   medidores das faixas) e aceita REC, STOP e PLAY de quem esta ligado a esse Wi-Fi (a palavra-passe e a autorizacao).
//     GET  /api/f6/lite                 estado curto (ligado, a gravar, tempo, faixas armadas, medidores): para a pagina
//   Voltar ao Atom: o menu System da pagina ("...", POST /api/mode?m=f6|gopro, sem token so a partir deste modo) ou o Atom por USB.
//   Passar a este modo: o menu System da pagina do portal (AP de configuracao, com o S3 em modo GoPro ou F6), ou a consola do Atom.
// Placa: ESP32-S3, USB CDC on boot. Estados que a GoPro dá por BLE (Open GoPro, query 0x13):
//   2 bateria (barras), 8 a gravar, 13 duracao do clip (s), 35 tempo de video que falta (s),
//   70 bateria (%).

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <Preferences.h>
#include <Update.h>
#include <NimBLEDevice.h>
#include "esp_ota_ops.h"
#include "usb/usb_host.h"      // modo F6: o S3 e o host USB do F6 (FRC-8)
#include "esp_task_wdt.h"
#include <vector>
#include <string>

#define MAX_CAMS 3

// Placa: M5Stack StampS3 (Stamp-S3A). LED RGB (WS2812B) nos dados no GPIO 21; alimentacao do LED no GPIO 38
// (tem de ficar a HIGH). Igual ao que o main.py do FIZ/BM ja usa na mesma placa.
#define LED_PIN 21
#define LED_POWER_PIN 38
#define LED_BRIGHT 40            // o LED e forte: 40/255 chega para se ver de dia
#define LED_BOOT_MS 15000        // azul nos primeiros 15 s (tempo de ligar as GoPro)
#define LED_BLINK_MS 500         // meio periodo do amarelo a piscar
static const char *ledEstado = "boot";   // "boot" | "ok" | "miss" (ver ledUpdate); vai no /api/status
static const uint16_t PORT_BEACON = 5170;

static const char *UUID_CMD = "b5f90072-aa8d-11e3-9046-0002a5d5c51b";     // comandos (W)
static const char *UUID_CMD_RSP = "b5f90073-aa8d-11e3-9046-0002a5d5c51b";  // respostas (N)
static const char *UUID_SET = "b5f90074-aa8d-11e3-9046-0002a5d5c51b";     // definicoes (W)
static const char *UUID_QRY = "b5f90076-aa8d-11e3-9046-0002a5d5c51b";     // consultas (W)
static const char *UUID_QRY_RSP = "b5f90077-aa8d-11e3-9046-0002a5d5c51b";  // respostas (N)

struct Cam {
  bool used = false;
  char id[6] = "";
  String name, mac;
  uint8_t type = 1;
  NimBLEClient *cli = nullptr;
  NimBLERemoteCharacteristic *chCmd = nullptr, *chQry = nullptr, *chSet = nullptr;
  bool connected = false;
  bool everUp = false;    // ja esteve ligada desde o arranque (LED: se falta agora, amarelo a piscar)
  int bat = -1;           // %
  int rssi = 0;           // sinal Bluetooth da ligacao (dBm; 0 = ainda nao lido)
  int recording = -1;     // -1 desconhecido
  long recS = -1, remS = -1;
  uint32_t lastStatusMs = 0, lastPollMs = 0, lastKeepMs = 0, retryAtMs = 0;
  uint32_t connectedAtMs = 0;
  int fails = 0;
  String err;
  String fw, model;       // da Device Information (0x180A): firmware e modelo
  // Relogio da camara (Open GoPro 'Get Date/Time', 0x0E): resolucao de 1 s, para conferir o acerto por QR.
  uint16_t clkY = 0; uint8_t clkMo = 0, clkD = 0, clkH = 0, clkMi = 0, clkS = 0;
  uint32_t clkAtMs = 0, clkSeq = 0;
  volatile bool clkReq = false;
  // Medida fina do relogio: pede-se a hora varias vezes por segundo ate o segundo mudar. O instante da
  // mudanca (entre a ultima resposta antiga e a primeira nova) da a fase do relogio da camara, ao ~0,1 s.
  volatile bool fineReq = false;
  bool fineDone = true, fineOk = false, fineInt = false;   // fineInt: parou porque ha um REC pedido
  uint32_t fineSeq = 0, fineLo = 0, fineHi = 0;
  uint16_t fnY = 0; uint8_t fnMo = 0, fnD = 0, fnH = 0, fnMi = 0, fnS = 0;
  // comando em curso (preenchido pelo HTTP, tratado pela tarefa BLE)
  volatile int wantRec = -1;   // -1 nada, 0 parar, 1 gravar
  volatile int cmdState = 0;   // 0 livre, 1 pedido na fila, 2 a executar
  uint32_t cmdSeq = 0;         // numero do ultimo comando (o bridge confirma pelo estado, com este numero)
  bool cmdDone = true;
  bool cmdOk = false;
  String cmdMsg;
  uint32_t cmdMs = 0;          // quanto demorou
};
static Cam cams[MAX_CAMS];
static SemaphoreHandle_t mtx;
static volatile uint32_t lastSeq = 0;
static Preferences prefs;
static WebServer server(80);
static WiFiUDP udp, udpRx;
static const uint16_t PORT_RESET = 5171;              // "GPGW-RESET" por UDP reinicia o gateway (quando o HTTP nao responde)
static volatile uint32_t bleTickMs = 0;               // a tarefa BLE actualiza-o; se parar, o vigia reinicia
static String otaErr;                                  // erro do OTA em curso (vazio = a correr bem)
static bool otaRecusado = false;                      // recusado por haver uma GoPro a gravar (409)
static volatile uint32_t rebootAtMs = 0;              // reinicio pedido (da-se tempo para a resposta HTTP sair)
static String gwToken;                                 // vazio: sem token (aceita tudo, como antes)
static int otaCodigo = 500;                            // o codigo HTTP de uma OTA recusada
static bool otaConcluido = false;                      // um envio multipart chegou ao fim e foi gravado (sem ele o POST /api/fw nao reinicia)

// ---- Wi-Fi: redes guardadas, tentativa por ordem, AP de configuracao quando nenhuma serve ----
enum WifiState { WSTATE_TRY, WSTATE_UP, WSTATE_AP };
static const uint32_t WIFI_TRY_MS = 15000;            // por rede
static const uint32_t WIFI_LOST_MS = 30000;           // ligado e perdido ha tanto: volta a tentar as redes
static const uint32_t WIFI_AP_MS = 180000;            // quanto o AP fica antes de se tentarem de novo as redes guardadas
static WifiState wifiSt = WSTATE_TRY;
static String wifiSsid[2], wifiPsk[2];
static int wifiNets = 0, wifiTryIdx = 0;
static bool wifiMigrar = false;                       // a tentar as credenciais do firmware anterior (guardadas pelo proprio Wi-Fi)
static uint32_t wifiStateMs = 0;
static String apName;

struct Found { String mac, name; uint8_t type; int rssi; bool conn; };
static std::vector<Found> found;
static volatile int scanReq = 0;      // segundos pedidos
static volatile bool scanDone = true;

#define LOCK() xSemaphoreTake(mtx, portMAX_DELAY)
#define UNLOCK() xSemaphoreGive(mtx)

// Registo de diagnostico (as ultimas linhas, em GET /api/log): quando um REC falha, e aqui que se ve porque.
#define LOG_N 60
static String logRing[LOG_N];
static uint32_t logCount = 0;
static bool f6HostLigado = false;     // com o USB host do modo F6 a serie deixa de existir (ver f6Start)
static void glog(const char *fmt, ...) {
  char b[200];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(b, sizeof b, fmt, ap);
  va_end(ap);
  if (!f6HostLigado) Serial.printf("LOG %s\n", b);
  xSemaphoreTake(mtx, portMAX_DELAY);
  logRing[logCount % LOG_N] = String(millis() / 1000.0, 1) + "s " + b;
  logCount++;
  xSemaphoreGive(mtx);
}

// -------------------------------------------------------------------- token ---
// Tipo de LED (guardado em "gw"/"led", mesmo .bin para todas as placas S3): 1 = RGB WS2812 no LED_PIN com a alimentacao no
// LED_POWER_PIN (a M5Stack StampS3, por omissao), 2 = LED simples no LED_PIN, acende a 0 (a XIAO ESP32S3), 0 = sem LED.
// Muda-se por USB (serie: LED rgb|simple|off) ou pelo Atom (consola: GATEWAYS > S3 > LED type).
static uint8_t ledTipo = 1;
static bool ledForcar = false;                         // redesenha o LED na proxima volta (mudou o tipo)
static const char *ledTipoNome(uint8_t t) { return t == 1 ? "rgb" : (t == 2 ? "simple" : "off"); }
static int ledTipoDe(String s) { s.trim(); s.toLowerCase(); return s == "rgb" ? 1 : (s == "simple" ? 2 : (s == "off" ? 0 : -1)); }
static void ledTipoAplicar(uint8_t t) {
  ledTipo = t;
  if (t == 1) { pinMode(LED_POWER_PIN, OUTPUT); digitalWrite(LED_POWER_PIN, HIGH); }
  else if (t == 2) { pinMode(LED_PIN, OUTPUT); digitalWrite(LED_PIN, HIGH); }
  ledForcar = true;
}

static void tokenLoad() {
  Preferences tp;
  tp.begin("sec", true);
  gwToken = tp.getString("tok", "");
  tp.end();
}

// Comparacao em tempo constante (o tempo nao diz quantos caracteres estao certos).
static bool tokenIgual(const String &got) {
  if (!gwToken.length()) return true;
  size_t n = gwToken.length();
  uint8_t r = got.length() != n;
  for (size_t i = 0; i < n; i++) r |= (uint8_t)gwToken[i] ^ (uint8_t)(i < got.length() ? got[i] : 0);
  return r == 0;
}

static bool tokenOk() { return tokenIgual(server.header("X-GW-Token")); }

// ------------------------------------------------------------------ guardar ---
static void saveCams() {
  prefs.begin("gw", false);
  for (int i = 0; i < MAX_CAMS; i++) {
    String k = String(i);
    if (cams[i].used) {
      prefs.putString(("m" + k).c_str(), cams[i].mac);
      prefs.putString(("n" + k).c_str(), cams[i].name);
      prefs.putUChar(("t" + k).c_str(), cams[i].type);
    } else {
      prefs.remove(("m" + k).c_str());
    }
  }
  prefs.end();
}

static void loadCams() {
  prefs.begin("gw", true);
  for (int i = 0; i < MAX_CAMS; i++) {
    String k = String(i);
    String m = prefs.getString(("m" + k).c_str(), "");
    if (m.length() == 17) {
      cams[i].used = true;
      snprintf(cams[i].id, sizeof cams[i].id, "gp%d", i + 1);
      cams[i].mac = m;
      cams[i].name = prefs.getString(("n" + k).c_str(), "GoPro");
      cams[i].type = prefs.getUChar(("t" + k).c_str(), 1);
    }
  }
  prefs.end();
}

// ---------------------------------------------------------------- utilitarios ---
static String hexStr(const uint8_t *d, size_t n) {
  String s;
  char b[4];
  for (size_t i = 0; i < n; i++) { snprintf(b, sizeof b, "%02x", d[i]); s += b; }
  return s;
}

static String jstr(const String &s) {
  String o = "\"";
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '"' || c == '\\') { o += '\\'; o += c; }
    else if ((uint8_t)c < 0x20) o += ' ';
    else o += c;
  }
  return o + "\"";
}

static int camIndex(const String &id) {
  for (int i = 0; i < MAX_CAMS; i++) if (cams[i].used && id == cams[i].id) return i;
  return -1;
}

// ------------------------------------------------------------ respostas da GoPro ---
// Resposta a uma consulta de estado (0x13): [len, 0x13, status, (id, vlen, valor)...]
// As respostas maiores que o MTU chegam em varios pacotes: o 1.o diz o tamanho (5 ou 13 bits),
// os seguintes comecam por 0x80|contador. Junta-se tudo antes de ler.
static std::vector<uint8_t> rxBuf[MAX_CAMS];
static size_t rxLen[MAX_CAMS];

static void onQueryPayload(int i, const uint8_t *d, size_t n);

static void onQueryResp(int i, uint8_t *d, size_t n) {
  if (n < 1) return;
  if (d[0] & 0x80) {                                   // continuacao
    rxBuf[i].insert(rxBuf[i].end(), d + 1, d + n);
  } else {
    int tipo = (d[0] >> 5) & 3;
    size_t hdr = 1;
    if (tipo == 0) rxLen[i] = d[0] & 0x1f;
    else if (tipo == 1 && n >= 2) { rxLen[i] = ((d[0] & 0x1f) << 8) | d[1]; hdr = 2; }
    else if (tipo == 2 && n >= 3) { rxLen[i] = (d[1] << 8) | d[2]; hdr = 3; }
    else return;
    rxBuf[i].assign(d + hdr, d + n);
  }
  if (rxBuf[i].size() >= rxLen[i] && rxLen[i] > 0) {
    onQueryPayload(i, rxBuf[i].data(), rxLen[i]);
    rxBuf[i].clear();
    rxLen[i] = 0;
  }
}

// Resposta a uma consulta de estado: [0x13, status, (id, vlen, valor)...]
static void onQueryPayload(int i, const uint8_t *d, size_t n) {
  if (n < 2 || d[0] != 0x13 || d[1] != 0) return;
  LOCK();
  size_t p = 2;
  while (p + 2 <= n) {
    uint8_t id = d[p], vl = d[p + 1];
    if (p + 2 + vl > n) break;
    const uint8_t *v = d + p + 2;
    auto be = [&](int k) { long x = 0; for (int j = 0; j < k; j++) x = (x << 8) | v[j]; return x; };
    if (id == 70 && vl >= 1) cams[i].bat = v[0];
    else if (id == 8 && vl >= 1) cams[i].recording = v[0] ? 1 : 0;
    else if (id == 13 && vl >= 1) cams[i].recS = be(vl);
    else if (id == 35 && vl >= 1) cams[i].remS = be(vl);
    p += 2 + vl;
  }
  cams[i].lastStatusMs = millis();
  UNLOCK();
}

// ------------------------------------------------------------ liga e mantem ---
// Os textos (err, fw, model) le-os o HTTP noutra tarefa: mudam-se so com o lock (um String a ser copiado e trocado
// ao mesmo tempo pode estragar a memoria e reiniciar o gateway).
static void setErr(Cam &c, const String &e) { LOCK(); c.err = e; UNLOCK(); }

static bool bleConnect(int i) {
  Cam &c = cams[i];
  if (!c.cli) c.cli = NimBLEDevice::createClient();
  if (!c.cli) { setErr(c, "no BLE client available"); return false; }
  // 4 s: uma GoPro desligada prendia a tarefa BLE 8 s de cada vez (e o REC das outras esperava).
  c.cli->setConnectTimeout(4000);
  c.chCmd = c.chQry = c.chSet = nullptr;
  NimBLEAddress a(std::string(c.mac.c_str()), c.type);
  if (!c.cli->connect(a, true, false, true)) { setErr(c, "connect failed " + String(c.cli->getLastError())); return false; }
  if (!NimBLEDevice::isBonded(a)) c.cli->secureConnection();      // 1.a vez: emparelha (a camara em "GoPro App")
  NimBLERemoteService *svc = c.cli->getService("fea6");
  if (!svc) { setErr(c, "no FEA6 service"); c.cli->disconnect(); return false; }
  c.chCmd = svc->getCharacteristic(UUID_CMD);
  c.chQry = svc->getCharacteristic(UUID_QRY);
  c.chSet = svc->getCharacteristic(UUID_SET);
  {                                                  // firmware e modelo, para saber se tem o Labs
    NimBLERemoteService *di = c.cli->getService("180a");
    if (di) {
      NimBLERemoteCharacteristic *f = di->getCharacteristic("2a26"), *m = di->getCharacteristic("2a24");
      String fw = (f && f->canRead()) ? String(f->readValue().c_str()) : String();
      String model = (m && m->canRead()) ? String(m->readValue().c_str()) : String();
      LOCK();
      if (fw.length()) c.fw = fw;
      if (model.length()) c.model = model;
      UNLOCK();
    }
  }
  NimBLERemoteCharacteristic *qr = svc->getCharacteristic(UUID_QRY_RSP);
  NimBLERemoteCharacteristic *cr = svc->getCharacteristic(UUID_CMD_RSP);
  if (!c.chCmd || !c.chQry || !qr || !cr) { setErr(c, "FEA6 characteristics missing"); c.cli->disconnect(); return false; }
  bool s1 = qr->subscribe(true, [i](NimBLERemoteCharacteristic *, uint8_t *d, size_t n, bool) {
    Serial.printf("LOG %s query response %s\n", cams[i].id, hexStr(d, n).c_str());
    onQueryResp(i, d, n);
  });
  bool s2 = cr->subscribe(true, [i](NimBLERemoteCharacteristic *, uint8_t *d, size_t n, bool) {
    Serial.printf("LOG %s cmd response %s\n", cams[i].id, hexStr(d, n).c_str());
    if (n >= 11 && d[1] == 0x0e && d[2] == 0x00) {            // [len, 0x0e, 0, 8, ano(2), mes, dia, h, m, s, ...]
      LOCK();
      cams[i].clkY = (d[4] << 8) | d[5]; cams[i].clkMo = d[6]; cams[i].clkD = d[7];
      cams[i].clkH = d[8]; cams[i].clkMi = d[9]; cams[i].clkS = d[10];
      cams[i].clkAtMs = millis(); cams[i].clkSeq++;
      UNLOCK();
    }
  });
  Serial.printf("LOG %s subscribed qry=%d cmd=%d bonded=%d\n", c.id, s1, s2, NimBLEDevice::isBonded(a));
  LOCK();
  c.connected = true;
  c.everUp = true;                     // ja esteve ligada desde o arranque: se cair, o LED fica a piscar
  c.err = "";
  c.fails = 0;
  c.connectedAtMs = millis();
  UNLOCK();
  glog("%s connected", c.id);
  return true;
}

static void pollStatus(Cam &c, bool soValor8 = false) {
  if (soValor8) {
    const uint8_t q[] = {0x03, 0x13, 0x08, 0x0d};
    c.chQry->writeValue(q, sizeof q, true);
  } else {
    const uint8_t q[] = {0x06, 0x13, 0x46, 0x08, 0x0d, 0x23, 0x02};   // 70, 8, 13, 35, 2
    static int n = 0;
    bool ok = c.chQry->writeValue(q, sizeof q, true);
    if (!ok || n++ < 2) Serial.printf("LOG %s poll write=%d\n", c.id, ok);
  }
}

static bool recPendente();

// Pede a hora da camara em cadeia (uma resposta a seguir a outra) ate o segundo mudar, ate 4 s.
static void doFineClock(int i) {
  Cam &c = cams[i];
  const uint8_t q[] = {0x01, 0x0e};
  uint32_t t0 = millis();
  LOCK(); uint32_t seq0 = c.clkSeq; UNLOCK();
  int prevS = -1; uint32_t prevAt = 0;
  bool achou = false;
  while (millis() - t0 < 4000 && !achou && !recPendente()) {      // um REC pedido interrompe a medida
    if (!c.chCmd->writeValue(q, sizeof q, true)) break;
    uint32_t espera = millis();
    while (millis() - espera < 400) {                      // a resposta chega pelo callback
      LOCK(); uint32_t sq = c.clkSeq; UNLOCK();
      if (sq != seq0 || recPendente()) break;
      delay(3);
    }
    LOCK();
    bool nova = c.clkSeq != seq0;
    uint32_t at = c.clkAtMs; int s = c.clkS;
    uint16_t y = c.clkY; uint8_t mo = c.clkMo, d = c.clkD, h = c.clkH, mi = c.clkMi;
    seq0 = c.clkSeq;
    UNLOCK();
    if (!nova) continue;
    if (prevS >= 0 && s != prevS) {                        // o segundo mudou entre a resposta anterior e esta
      LOCK();
      c.fineLo = prevAt; c.fineHi = at;
      c.fnY = y; c.fnMo = mo; c.fnD = d; c.fnH = h; c.fnMi = mi; c.fnS = s;
      UNLOCK();
      achou = true;
    }
    prevS = s; prevAt = at;
  }
  LOCK(); c.fineOk = achou; c.fineInt = !achou && recPendente(); c.fineDone = true; UNLOCK();
  glog("%s fine clock: %s (%u ms)", c.id, achou ? "flip found" : "no flip", (unsigned)(millis() - t0));
}

// REC: primeiro escreve-se o comando em TODAS as camaras com pedido, so depois se confirma (antes cada camara esperava
// ate ~6 s pela confirmacao da anterior: com 3 GoPro a ultima arrancava segundos depois da primeira).
static bool recPendente() {
  for (int i = 0; i < MAX_CAMS; i++)
    if (cams[i].used && cams[i].connected && cams[i].cmdState == 1 && cams[i].wantRec >= 0) return true;
  return false;
}

static void doRecTodos() {
  bool ativo[MAX_CAMS] = {false}, escrito[MAX_CAMS] = {false}, conf[MAX_CAMS] = {false};
  int want[MAX_CAMS];
  uint32_t t0 = millis(), tw[MAX_CAMS] = {0};
  for (int i = 0; i < MAX_CAMS; i++) {
    Cam &c = cams[i];
    if (!(c.used && c.connected && c.cmdState == 1 && c.wantRec >= 0 && c.chCmd)) continue;
    ativo[i] = true;
    want[i] = c.wantRec;
    LOCK(); c.cmdState = 2; UNLOCK();
    const uint8_t cmd[] = {0x03, 0x01, 0x01, (uint8_t)(want[i] ? 1 : 0)};
    escrito[i] = c.chCmd->writeValue(cmd, sizeof cmd, true);
    tw[i] = millis() - t0;
  }
  // Confirmar todas juntas: ate ~6 s (a camara demora ~1,5 s a mudar o estado no STOP)
  for (int k = 0; k < 45; k++) {
    bool falta = false;
    for (int i = 0; i < MAX_CAMS; i++) {
      if (!ativo[i] || !escrito[i] || conf[i]) continue;
      if (cams[i].connected && cams[i].chQry) pollStatus(cams[i], true);
      falta = true;
    }
    if (!falta) break;
    delay(130);
    bleTickMs = millis();
    for (int i = 0; i < MAX_CAMS; i++) {
      if (!ativo[i] || !escrito[i] || conf[i]) continue;
      LOCK();
      conf[i] = (cams[i].recording == (want[i] ? 1 : 0)) && (millis() - cams[i].lastStatusMs < 500);
      UNLOCK();
    }
  }
  uint32_t dt = millis() - t0;
  for (int i = 0; i < MAX_CAMS; i++) {
    if (!ativo[i]) continue;
    Cam &c = cams[i];
    LOCK();
    c.cmdOk = escrito[i] && conf[i];
    c.cmdMsg = !escrito[i] ? "write failed" : (conf[i] ? "confirmed" : "no confirmation from the camera");
    c.cmdMs = dt;
    c.cmdDone = true;
    c.cmdState = 0;
    c.wantRec = -1;
    UNLOCK();
    glog("%s rec %d: write=%d (%u ms) confirmed=%d total=%u ms", c.id, want[i], escrito[i], (unsigned)tw[i], conf[i], (unsigned)dt);
  }
}

class ScanCb : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice *d) override {
    if (!d->isAdvertisingService(NimBLEUUID("FEA6"))) return;
    String mac = String(d->getAddress().toString().c_str());
    LOCK();
    for (auto &f : found) if (f.mac == mac) { f.rssi = d->getRSSI(); UNLOCK(); return; }
    found.push_back({mac, String(d->getName().c_str()), d->getAddress().getType(), d->getRSSI(), d->isConnectable()});
    UNLOCK();
  }
} scanCb;

static void bleTask(void *) {
  for (;;) {
    bleTickMs = millis();
    // Um REC pedido passa a frente de tudo, tambem da medida do relogio (que ocupa a ligacao ate 4 s).
    if (recPendente()) doRecTodos();
    for (int i = 0; i < MAX_CAMS; i++)
      if (cams[i].used && cams[i].connected && cams[i].fineReq && cams[i].chCmd) {
        cams[i].fineReq = false;
        doFineClock(i);
      }
    for (int i = 0; i < MAX_CAMS; i++)
      if (cams[i].used && cams[i].connected && cams[i].clkReq && cams[i].chCmd) {
        cams[i].clkReq = false;
        const uint8_t q[] = {0x01, 0x0e};
        cams[i].chCmd->writeValue(q, sizeof q, true);
      }
    // Os comandos de REC passam sempre primeiro: nunca esperam por uma procura ou por uma
    // tentativa de ligacao a outra camara.
    if (recPendente()) doRecTodos();
    if (scanReq > 0) {
      int s = scanReq;
      LOCK(); scanReq = 0; found.clear(); scanDone = false; UNLOCK();
      NimBLEScan *sc = NimBLEDevice::getScan();
      sc->setScanCallbacks(&scanCb, false);
      sc->setActiveScan(true);
      sc->setInterval(100);
      sc->setWindow(99);
      sc->clearResults();
      sc->start(s * 1000, false, true);
      uint32_t fim = millis() + s * 1000UL + 500;
      while (sc->isScanning() && millis() < fim && !recPendente() && scanReq == 0) { delay(50); bleTickMs = millis(); }
      sc->stop();
      // Pedido novo a meio: esta procura (velha, truncada) deita-se fora e o done fica a false ate a nova acabar.
      LOCK(); if (scanReq > 0) found.clear(); else scanDone = true; UNLOCK();
    }
    for (int i = 0; i < MAX_CAMS; i++) {
      Cam &c = cams[i];
      if (!c.used) continue;
      uint32_t now = millis();
      if (c.cli && c.connected && !c.cli->isConnected()) {
        // Uma medida fina ou um pedido de hora pendentes morrem com a ligacao: senao o fineDone ficava a false (409 para
        // sempre) ou a medida abandonada corria ao voltar.
        LOCK(); c.connected = false; c.recording = -1; c.err = "disconnected"; c.retryAtMs = now + 2000;
        c.fineReq = false; c.fineDone = true; c.fineOk = false; c.clkReq = false; UNLOCK();
        glog("%s disconnected", c.id);
      }
      if (!c.connected) {
        if (now >= c.retryAtMs && !recPendente()) {     // um REC pedido passa a frente de uma tentativa de ligacao
          if (!bleConnect(i)) {
            c.fails++;
            c.retryAtMs = millis() + min(30000, 2000 * (c.fails + 1));    // recua ate 30 s
            LOCK(); String e = c.err; UNLOCK();
            glog("%s %s (retry in %d ms)", c.id, e.c_str(), (int)(c.retryAtMs - millis()));
          }
          bleTickMs = millis();
        }
        if (c.cmdState == 1) {                           // pedido de REC sem ligacao
          LOCK(); c.cmdOk = false; c.cmdMsg = "camera not connected"; c.cmdDone = true; c.cmdState = 0; c.wantRec = -1; UNLOCK();
        }
        continue;
      }
      if (now - c.lastPollMs >= 2000) {                  // estado de 2 em 2 s
        c.lastPollMs = now;
        pollStatus(c);
        int r = c.cli->getRssi();                       // o sinal da ligacao: serve para escolher onde pousar o S3
        LOCK(); c.rssi = r; UNLOCK();
      }
      if (c.chSet && now - c.lastKeepMs >= 3000) {       // mantem a camara acordada (Open GoPro keep-alive)
        c.lastKeepMs = now;
        const uint8_t ka[] = {0x03, 0x5b, 0x01, 0x42};
        c.chSet->writeValue(ka, sizeof ka, true);
      }
    }
    delay(50);
  }
}

// -------------------------------------------------------------------- JSON ---
// =========================================================== modo F6 (FRC-8) ===
// O mesmo S3, em vez das GoPro, faz de comando FRC-8 do Zoom F6 (selector: POST /api/mode?m=gopro|f6, ou MODE pela
// serie; fica guardado e reinicia). Com o mesmo endereco e o mesmo token: o bridge le o F6 no /api/f6 e manda REC/STOP
// com /api/f6/press. Sem Bluetooth neste modo (as GoPro ficam guardadas para quando voltar ao modo GoPro).
//
// O F6 (MENU > SYSTEM > USB > FRC-8) e um aparelho USB-MIDI e alimenta o S3: o S3 e o host USB, como o FRC-8. Ordem que
// funciona (2026-10-15): o cabo do F6 ao S3 primeiro, e so depois connect no F6. O USB host arranca logo no setup() (o F6
// so espera ~2 s) e o Wi-Fi so depois de o F6 estar ligado (ou 8 s), com menos potencia: os picos de corrente do radio
// na USB do F6 desligavam o S3.
// Ligado ao Atom em vez do F6: ao fim de 10 s sem nada no USB reinicia uma vez com a porta serie (configuracao por USB).
//
// Protocolo (frc8.py, confirmado no F6 a 2026-10-08/13): pedido de ligacao F0 52 00 74 00 00 00 63 00 01 F7;
// 100 ms depois identificacao (63 03) e alimentacao (63 0F), e o 63 0F + FE de 0,25 em 0,25 s (sem isso o F6 corta).
// Botoes: nota 90 nn 7F/00 (REC 24, STOP 22, PLAY 23). Estado: 90 29 (a gravar), 90 28 (play), 90/8A 00-07 (pistas
// armadas), Dn v (medidor da pista n, 0..12), SysEx 63 16 01/00 (caixa de texto aberta/fechada).
static bool modoF6 = false;            // o selector (guardado em "gw"/"mode"): 1 = F6 com o Atom, 2 = F6 autonomo
static bool soloF6 = false;            // F6 autonomo: o S3 e o ponto de acesso (WPA2) e serve a pagina do comando
static String soloPsk;                 // a palavra-passe desse Wi-Fi (guardada em "gw"/"solopsk")
static bool f6Host = false;            // o USB host esta a correr (a serie deixou de existir)
static volatile bool f6BusVisto = false;   // ja apareceu um aparelho no USB (o F6) desde o arranque
// Ligado ao Atom em vez do F6 (configuracao por USB): no barramento nunca aparece nada. Ao fim de F6_SEM_BUS_MS o S3
// reinicia UMA vez sem o USB host, com a porta serie; esta marca sobrevive ao reinicio por software, nao a desligar.
#define F6_SEM_BUS_MS 10000
#define F6_SERIE_MAGIC 0x5E41A1F6u
static RTC_NOINIT_ATTR uint32_t f6SerieUmaVez;
static volatile bool f6Usb = false, f6Sync = false;
static volatile int f6Rec = -1, f6Play = -1;
static volatile uint8_t f6Arms = 0;
static volatile bool f6ArmsKnown = false, f6Kbd = false;
static volatile uint8_t f6Meter[10] = {0};
static volatile int16_t f6Fader[9] = {-1, -1, -1, -1, -1, -1, -1, -1, -1};   // 92 nn vv: o F6 diz o fader (8 = master)
// As ultimas mensagens do F6 (sem os medidores), para testes: o que o F6 respondeu a um comando.
#define F6_RX_N 24
static portMUX_TYPE f6RxMux = portMUX_INITIALIZER_UNLOCKED;
static uint8_t f6RxLog[F6_RX_N][13];      // [0] = tamanho, depois os bytes (SysEx cortado aos 12)
static volatile uint32_t f6RxSeq = 0;

static void f6RxGuardar(const uint8_t *b, int n) {
  if (n > 12) n = 12;
  portENTER_CRITICAL(&f6RxMux);
  uint8_t *e = f6RxLog[f6RxSeq % F6_RX_N];
  e[0] = n;
  memcpy(e + 1, b, n);
  f6RxSeq++;
  portEXIT_CRITICAL(&f6RxMux);
}
static volatile uint32_t f6RxMs = 0, f6OpenMs = 0, f6RecSinceMs = 0;
static volatile uint16_t f6Vid = 0, f6Pid = 0;
static usb_host_client_handle_t f6Client = NULL;
static usb_device_handle_t f6Dev = NULL;
static uint8_t f6EpIn = 0, f6EpOut = 0;
static uint16_t f6MpsIn = 64;
static int f6Itf = -1;
static usb_transfer_t *f6XIn = NULL, *f6XOut = NULL;
static volatile bool f6InBusy = false, f6OutBusy = false, f6Gone = false;
static volatile uint8_t f6NewAddr = 0;
static QueueHandle_t f6Txq;
static uint8_t f6Sx[24];               // o inicio do SysEx que esta a chegar (so interessa o 63 16)
static int f6SxN = -1;

static const uint8_t F6_HS[] = {0xF0, 0x52, 0x00, 0x74, 0x00, 0x00, 0x00, 0x63, 0x00, 0x01, 0xF7};
static const uint8_t F6_IDENT[] = {0xF0, 0x52, 0x00, 0x74, 0x00, 0x00, 0x00, 0x63, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF7};
static const uint8_t F6_POWER[] = {0xF0, 0x52, 0x00, 0x74, 0x00, 0x00, 0x00, 0x63, 0x0F, 0x01, 0x00, 0x09, 0x30, 0x09, 0x30, 0x00, 0xF7};

static void f6Queue(uint8_t a, uint8_t b, uint8_t c, uint8_t d) {
  uint32_t p = a | (b << 8) | (c << 16) | ((uint32_t)d << 24);
  xQueueSend(f6Txq, &p, 0);
}

// Uma mensagem MIDI (canal, tempo real ou SysEx completo) -> pacotes USB-MIDI, cabo 0.
static void f6Send(const uint8_t *m, int n) {
  if (n <= 0 || !f6Txq) return;
  if (m[0] == 0xF0) {
    int i = 0;
    while (i < n) {
      int k = n - i < 3 ? n - i : 3;
      bool last = (i + k >= n);
      uint8_t cin = !last ? 0x4 : (k == 1 ? 0x5 : (k == 2 ? 0x6 : 0x7));
      f6Queue(cin, m[i], k > 1 ? m[i + 1] : 0, k > 2 ? m[i + 2] : 0);
      i += k;
    }
  } else if (m[0] >= 0xF8) {
    f6Queue(0x0F, m[0], 0, 0);
  } else {
    f6Queue(m[0] >> 4, m[0], n > 1 ? m[1] : 0, n > 2 ? m[2] : 0);
  }
}

#define F6_NOTE_PLAY 0x23                    // botao PLAY do FRC-8 (90 23 7F/00)
static void f6Press(uint8_t note) {
  uint8_t on[3] = {0x90, note, 0x7F}, off[3] = {0x90, note, 0x00};
  f6Send(on, 3);
  delay(80);
  f6Send(off, 3);
}

static void f6Msg(uint8_t s, uint8_t d1, uint8_t d2) {
  f6RxMs = millis();
  if (s == 0x90 && d1 == 0x29) {
    int v = d2 > 0;
    if (v != f6Rec) { f6Rec = v; f6RecSinceMs = v ? millis() : 0; glog("f6: REC %s", v ? "ON" : "OFF"); }
    // As pistas armadas so se sabem pelas notas das proprias pistas (como no Atom por USB): o estado de REC nao as
    // diz, e marcar aqui "armed: []" fazia o Atom ler "nenhuma armada" e bloquear o REC, sem o pedido de reinicio.
  } else if (s == 0x90 && d1 == 0x28) {
    f6Play = d2 > 0;
  } else if ((s == 0x90 || s == 0x8A) && d1 < 8) {
    f6Arms = (d2 > 0) ? (f6Arms | (1 << d1)) : (f6Arms & ~(1 << d1));
    f6ArmsKnown = true;
  } else if ((s & 0xF0) == 0xD0 && (s & 0x0F) < 10) {
    f6Meter[s & 0x0F] = d1;
    return;                                   // os medidores nao vao ao registo das mensagens
  } else if (s == 0x92 && d1 < 9) {
    f6Fader[d1] = d2;
  }
  uint8_t m[3] = {s, d1, d2};
  f6RxGuardar(m, ((s & 0xF0) == 0xC0) ? 2 : 3);
}

static void f6SysexByte(uint8_t x) {
  if (x == 0xF0) { f6SxN = 0; }
  if (f6SxN < 0) return;
  if (f6SxN < (int)sizeof f6Sx) f6Sx[f6SxN] = x;
  f6SxN++;
  if (x == 0xF7) {
    if (!(f6SxN >= 9 && f6Sx[8] == 0x0F)) f6RxGuardar(f6Sx, f6SxN < 12 ? f6SxN : 12);   // sem o 63 0F (sinal de vida)
    if (f6SxN >= 11 && f6Sx[7] == 0x63 && f6Sx[8] == 0x16) {
      bool aberta = f6Sx[9] == 0x01;
      if (aberta != f6Kbd) { f6Kbd = aberta; glog("f6: text box %s", aberta ? "open" : "closed"); }
    }
    f6SxN = -1;
  }
}

static void f6InCb(usb_transfer_t *t) {
  f6InBusy = false;
  if (t->status == USB_TRANSFER_STATUS_COMPLETED) {
    for (int i = 0; i + 3 < t->actual_num_bytes; i += 4) {
      uint8_t *p = t->data_buffer + i;
      uint8_t cin = p[0] & 0x0F;
      if (cin >= 0x8 && cin <= 0xE) f6Msg(p[1], p[2], p[3]);
      else if (cin >= 0x4 && cin <= 0x7) {
        int n = cin == 0x5 ? 1 : (cin == 0x6 ? 2 : 3);
        for (int k = 0; k < n; k++) f6SysexByte(p[1 + k]);
        f6RxMs = millis();
      } else if (cin != 0) f6RxMs = millis();
    }
  }
  if (f6Usb && !f6Gone && usb_host_transfer_submit(t) == ESP_OK) f6InBusy = true;
}

static void f6OutCb(usb_transfer_t *t) {
  f6OutBusy = false;
  if (t->status != USB_TRANSFER_STATUS_COMPLETED) glog("f6: USB OUT failed status=%d", t->status);
}

static void f6ClientCb(const usb_host_client_event_msg_t *msg, void *arg) {
  if (msg->event == USB_HOST_CLIENT_EVENT_NEW_DEV) f6NewAddr = msg->new_dev.address;
  else if (msg->event == USB_HOST_CLIENT_EVENT_DEV_GONE) f6Gone = true;
}

static void f6Open(uint8_t addr) {
  f6BusVisto = true;
  if (usb_host_device_open(f6Client, addr, &f6Dev) != ESP_OK) { glog("f6: USB open failed addr=%d", addr); f6Dev = NULL; return; }
  const usb_device_desc_t *dd;
  usb_host_get_device_descriptor(f6Dev, &dd);
  f6Vid = dd->idVendor; f6Pid = dd->idProduct;
  const usb_config_desc_t *cd;
  usb_host_get_active_config_descriptor(f6Dev, &cd);
  const uint8_t *p = (const uint8_t *)cd;
  int total = cd->wTotalLength, cls = 0, sub = 0, alt = 0, cur = -1;
  f6EpIn = f6EpOut = 0; f6Itf = -1;
  for (int i = 0; i + 1 < total; ) {
    uint8_t len = p[i], type = p[i + 1];
    if (len < 2) break;
    if (type == 0x04) { cur = p[i + 2]; alt = p[i + 3]; cls = p[i + 5]; sub = p[i + 6]; }
    else if (type == 0x05 && cls == 1 && sub == 3 && alt == 0 && (p[i + 3] & 3) == 2) {
      if (p[i + 2] & 0x80) { f6EpIn = p[i + 2]; f6MpsIn = p[i + 4] | (p[i + 5] << 8); }
      else f6EpOut = p[i + 2];
      f6Itf = cur;
    }
    i += len;
  }
  glog("f6: USB device %04x:%04x MIDI interface %d", f6Vid, f6Pid, f6Itf);
  if (f6Itf < 0 || !f6EpIn || !f6EpOut) { glog("f6: no MIDI interface (is the F6 in FRC-8 mode?)"); return; }
  if (usb_host_interface_claim(f6Client, f6Dev, f6Itf, 0) != ESP_OK) { glog("f6: claim failed"); return; }
  if (!f6XIn) usb_host_transfer_alloc(f6MpsIn, 0, &f6XIn);
  if (!f6XOut) usb_host_transfer_alloc(64, 0, &f6XOut);
  f6XIn->device_handle = f6Dev; f6XIn->bEndpointAddress = f6EpIn; f6XIn->callback = f6InCb; f6XIn->num_bytes = f6MpsIn;
  f6XOut->device_handle = f6Dev; f6XOut->bEndpointAddress = f6EpOut; f6XOut->callback = f6OutCb;
  xQueueReset(f6Txq);
  f6Gone = false; f6Usb = true; f6Sync = false;
  f6Rec = f6Play = -1; f6Arms = 0; f6ArmsKnown = false; f6Kbd = false; f6SxN = -1; f6RecSinceMs = 0;
  for (int i = 0; i < 9; i++) f6Fader[i] = -1;
  f6OpenMs = millis();
  if (usb_host_transfer_submit(f6XIn) == ESP_OK) f6InBusy = true;
  f6Send(F6_HS, sizeof F6_HS);
  glog("f6: link request sent");
}

static void f6Close() {
  if (!f6Dev) return;
  f6Usb = false; f6Sync = false;
  if (f6EpIn) { usb_host_endpoint_halt(f6Dev, f6EpIn); usb_host_endpoint_flush(f6Dev, f6EpIn); usb_host_endpoint_clear(f6Dev, f6EpIn); }
  if (f6EpOut) { usb_host_endpoint_halt(f6Dev, f6EpOut); usb_host_endpoint_flush(f6Dev, f6EpOut); usb_host_endpoint_clear(f6Dev, f6EpOut); }
  uint32_t t0 = millis();
  while ((f6InBusy || f6OutBusy) && millis() - t0 < 500) usb_host_client_handle_events(f6Client, pdMS_TO_TICKS(10));
  if (f6Itf >= 0) usb_host_interface_release(f6Client, f6Dev, f6Itf);
  usb_host_device_close(f6Client, f6Dev);
  f6Dev = NULL;
  // O F6 saiu: o que se sabia dele ja nao vale. Com f6Rec a 1, gravaAlgo() recusava mudar de modo ou de firmware
  // ("recording: stop first") ate ele voltar, e o estado dizia que gravava um aparelho que nem esta ligado.
  f6Rec = f6Play = -1; f6Arms = 0; f6ArmsKnown = false; f6Kbd = false; f6RecSinceMs = 0;
  for (int i = 0; i < 10; i++) f6Meter[i] = 0;
  glog("f6: disconnected");
}

static void f6LibTask(void *) {
  for (;;) { uint32_t flags = 0; usb_host_lib_handle_events(portMAX_DELAY, &flags); }
}

static void f6ClientTask(void *) {
  usb_host_client_config_t cc = {};
  cc.is_synchronous = false;
  cc.max_num_event_msg = 5;
  cc.async.client_event_callback = f6ClientCb;
  cc.async.callback_arg = NULL;
  usb_host_client_register(&cc, &f6Client);
  uint32_t lastFe = 0, lastList = 0, lastPower = 0;
  bool reasked = false;
  for (;;) {
    usb_host_client_handle_events(f6Client, pdMS_TO_TICKS(5));
    if (millis() - lastList > 3000) {              // sem o evento de aparelho novo: abre o que estiver no barramento
      lastList = millis();
      uint8_t addrs[8]; int n = 0;
      usb_host_device_addr_list_fill(8, addrs, &n);
      if (n) f6BusVisto = true;
      if (n && !f6Dev && !f6NewAddr) f6NewAddr = addrs[0];
    }
    if (f6NewAddr) { uint8_t a = f6NewAddr; f6NewAddr = 0; f6Open(a); }
    if (f6Gone && f6Dev) { f6Close(); f6Gone = false; }
    if (!f6Usb) continue;
    uint32_t now = millis(), dt = now - f6OpenMs;
    if (now - lastFe >= 250) { lastFe = now; f6Queue(0x0F, 0xFE, 0, 0); }
    if (!f6Sync && dt >= 100) {
      f6Send(F6_IDENT, sizeof F6_IDENT);
      f6Send(F6_POWER, sizeof F6_POWER);
      lastPower = now; f6Sync = true; reasked = false;
      glog("f6: connected (identification and power sent)");
    }
    if (f6Sync && now - lastPower >= 250) { lastPower = now; f6Send(F6_POWER, sizeof F6_POWER); }
    // O F6 ja em sessao (o S3 reiniciou com o cabo ligado) nao manda o estado: pede-se o arranque outra vez, uma vez.
    if (f6Sync && !reasked && !f6ArmsKnown && dt >= 2000) {
      reasked = true;
      f6Send(F6_HS, sizeof F6_HS); f6Send(F6_IDENT, sizeof F6_IDENT); f6Send(F6_POWER, sizeof F6_POWER);
      glog("f6: no track state after 2 s, asking again");
    }
    if (!f6OutBusy && uxQueueMessagesWaiting(f6Txq)) {
      int n = 0;
      uint32_t pk;
      while (n < 16 && xQueueReceive(f6Txq, &pk, 0) == pdTRUE) { memcpy(f6XOut->data_buffer + 4 * n, &pk, 4); n++; }
      f6XOut->num_bytes = 4 * n;
      if (usb_host_transfer_submit(f6XOut) == ESP_OK) f6OutBusy = true;
      else glog("f6: USB OUT submit failed");
    }
  }
}

// Arranca o USB host (a serie USB deixa de existir: para voltar a gravar o S3, BOOT carregado ao ligar).
static void f6Start() {
  f6Txq = xQueueCreate(64, sizeof(uint32_t));
  f6Host = f6HostLigado = true;           // a serie fica como esta (o s3_frc8 nunca a fechava e o F6 ligava)
  usb_host_config_t hc = {};
  hc.skip_phy_setup = false;
  hc.intr_flags = ESP_INTR_FLAG_LEVEL1;
  esp_err_t r = usb_host_install(&hc);
  glog("f6: USB host %s", r == ESP_OK ? "ready" : "FAILED");
  xTaskCreatePinnedToCore(f6LibTask, "usbhost", 4096, NULL, 5, NULL, 0);
  xTaskCreatePinnedToCore(f6ClientTask, "usbclient", 6144, NULL, 5, NULL, 0);
}

static bool f6Ready() { return f6Usb && f6Sync; }

// O estado curto para a pagina do comando (o f6Json traz tambem as ultimas mensagens do F6, demasiado para 4 pedidos/s).
static String f6Lite() {
  String s = "{\"ok\":true,\"connected\":" + String(f6Ready() ? "true" : "false") +
             ",\"recording\":" + (f6Rec < 0 ? String("null") : String(f6Rec ? "true" : "false")) +
             ",\"rec_ms\":" + (f6Rec == 1 && f6RecSinceMs ? String(millis() - f6RecSinceMs) : String("null")) +
             ",\"playing\":" + (f6Play < 0 ? String("null") : String(f6Play ? "true" : "false")) + ",\"armed\":";
  if (f6ArmsKnown) {
    s += "[";
    bool first = true;
    for (int i = 0; i < 8; i++) if (f6Arms & (1 << i)) { s += String(first ? "" : ",") + String(i + 1); first = false; }
    s += "]";
  } else s += "null";
  s += ",\"meters\":[";
  for (int i = 0; i < 10; i++) s += String(i ? "," : "") + String(f6Meter[i]);
  return s + "]}";
}

static String f6Json() {
  String s = "{\"host\":" + String(f6Host ? "true" : "false") + ",\"usb\":" + String(f6Usb ? "true" : "false") +
             ",\"connected\":" + String(f6Ready() ? "true" : "false") +
             ",\"recording\":" + (f6Rec < 0 ? String("null") : String(f6Rec ? "true" : "false")) +
             ",\"rec_ms\":" + (f6Rec == 1 && f6RecSinceMs ? String(millis() - f6RecSinceMs) : String("null")) +
             ",\"playing\":" + (f6Play < 0 ? String("null") : String(f6Play ? "true" : "false")) + ",\"armed\":";
  if (f6ArmsKnown) {
    s += "[";
    bool first = true;
    for (int i = 0; i < 8; i++) if (f6Arms & (1 << i)) { s += String(first ? "" : ",") + String(i + 1); first = false; }
    s += "]";
  } else s += "null";
  s += ",\"meters\":[";
  for (int i = 0; i < 10; i++) s += String(i ? "," : "") + String(f6Meter[i]);
  char dev[12];
  snprintf(dev, sizeof dev, "%04x:%04x", f6Vid, f6Pid);
  s += "],\"faders\":[";
  for (int i = 0; i < 9; i++) s += String(i ? "," : "") + (f6Fader[i] < 0 ? String("null") : String(f6Fader[i]));
  // As ultimas mensagens do F6 (as mais velhas primeiro) e o numero da ultima: quem testa ve o que chegou de novo.
  s += "],\"rx_seq\":" + String((unsigned long)f6RxSeq) + ",\"rx\":[";
  {
    uint8_t copia[F6_RX_N][13];
    uint32_t seq;
    portENTER_CRITICAL(&f6RxMux);
    memcpy(copia, f6RxLog, sizeof copia);
    seq = f6RxSeq;
    portEXIT_CRITICAL(&f6RxMux);
    uint32_t n = seq < F6_RX_N ? seq : F6_RX_N;
    for (uint32_t k = 0; k < n; k++) {
      uint8_t *e = copia[(seq - n + k) % F6_RX_N];
      String h;
      for (int j = 0; j < e[0]; j++) { char t[4]; snprintf(t, sizeof t, j ? " %02x" : "%02x", e[1 + j]); h += t; }
      s += String(k ? "," : "") + jstr(h);
    }
  }
  s += "],\"kbd_open\":" + String(f6Kbd ? "true" : "false") + ",\"dev\":" + jstr(f6Vid ? String(dev) : String("")) +
       ",\"rx_age_ms\":" + (f6Usb && f6RxMs ? String(millis() - f6RxMs) : String("null")) + "}";
  return s;
}

static bool gravaAlgo() {                     // GoPro ou F6 a gravar: nao se muda de modo nem de firmware
  if (f6Rec == 1) return true;
  bool rec = false;
  LOCK();
  for (auto &c : cams) if (c.used && c.recording == 1) rec = true;
  UNLOCK();
  return rec;
}

static void modoGuardar(uint8_t modo, const String *psk = nullptr) {
  prefs.begin("gw", false);
  prefs.putUChar("mode", modo);
  if (psk) prefs.putString("solopsk", *psk);
  prefs.end();
}

static const char *ERRO_MODO_F6 = "the S3 is in F6 mode (console: GATEWAYS > S3 > Mode)";
#define SO_GOPRO() do { if (modoF6) { sendErr(409, ERRO_MODO_F6); return; } } while (0)

static String camJson(int i) {
  Cam &c = cams[i];
  String s = "{\"id\":" + jstr(c.id) + ",\"name\":" + jstr(c.name) + ",\"mac\":" + jstr(c.mac) +
             ",\"connected\":" + (c.connected ? "true" : "false");
  s += ",\"battery\":" + (c.bat >= 0 ? String(c.bat) : String("null"));
  s += ",\"rssi\":" + (c.connected && c.rssi < 0 ? String(c.rssi) : String("null"));
  s += ",\"recording\":" + (c.recording < 0 ? String("null") : String(c.recording ? "true" : "false"));
  s += ",\"rec_s\":" + (c.recS >= 0 ? String(c.recS) : String("null"));
  s += ",\"remaining_s\":" + (c.remS >= 0 ? String(c.remS) : String("null"));
  s += ",\"age_ms\":" + (c.lastStatusMs ? String(millis() - c.lastStatusMs) : String("null"));
  s += ",\"fw\":" + (c.fw.length() ? jstr(c.fw) : String("null"));
  s += ",\"clock\":" + (c.clkAtMs ? String("{\"seq\":") + String(c.clkSeq) + ",\"y\":" + String(c.clkY) + ",\"mo\":" + String(c.clkMo) +
       ",\"d\":" + String(c.clkD) + ",\"h\":" + String(c.clkH) + ",\"mi\":" + String(c.clkMi) + ",\"s\":" + String(c.clkS) +
       ",\"age_ms\":" + String(millis() - c.clkAtMs) + "}" : String("null"));
  s += ",\"fine\":{\"seq\":" + String(c.fineSeq) + ",\"state\":" + jstr(c.fineDone ? "done" : "running") +
       ",\"ok\":" + String(c.fineOk ? "true" : "false") + ",\"reason\":" + (c.fineInt && !c.fineOk ? String("\"interrupted by REC\"") : String("null")) + ",\"lo\":" + String(c.fineLo) + ",\"hi\":" + String(c.fineHi) +
       ",\"y\":" + String(c.fnY) + ",\"mo\":" + String(c.fnMo) + ",\"d\":" + String(c.fnD) + ",\"h\":" + String(c.fnH) +
       ",\"mi\":" + String(c.fnMi) + ",\"s\":" + String(c.fnS) + ",\"gw_now\":" + String(millis()) + "}";
  s += ",\"error\":" + (c.err.length() ? jstr(c.err) : String("null"));
  // O ultimo comando de REC: o bridge espera por state=done com o seq que lhe foi dado.
  s += ",\"cmd\":{\"seq\":" + String(c.cmdSeq) + ",\"state\":" +
       jstr(c.cmdDone ? "done" : (c.cmdState == 2 ? "running" : "queued")) +
       ",\"ok\":" + String(c.cmdOk ? "true" : "false") + ",\"msg\":" + jstr(c.cmdMsg) + ",\"ms\":" + String(c.cmdMs) + "}}";
  return s;
}

static String statusJson() {
  LOCK();
  String s = "{\"ok\":true,\"fw\":\"gopro_gw 21\",\"mode\":" + String(soloF6 ? "\"f6solo\"" : modoF6 ? "\"f6\"" : "\"gopro\"") + ",\"token\":" + String(gwToken.length() ? "true" : "false") + ",\"uptime_s\":" + String(millis() / 1000) + ",\"ip\":" + jstr(WiFi.localIP().toString()) +
             ",\"wifi\":" + jstr(wifiSt == WSTATE_UP ? "up" : (wifiSt == WSTATE_AP ? "ap" : "trying")) + ",\"ap\":" + jstr(apName) +
             ",\"wifi_rssi\":" + String(WiFi.RSSI()) + ",\"heap\":" + String(ESP.getFreeHeap()) +
             ",\"led\":" + jstr(ledEstado) + ",\"led_type\":" + jstr(ledTipoNome(ledTipo)) + (modoF6 ? ",\"f6\":" + f6Json() : String("")) + ",\"cams\":[";
  bool first = true;
  for (int i = 0; i < MAX_CAMS && !modoF6; i++) {     // no modo F6 as GoPro ficam guardadas, mas nao estao ca
    if (!cams[i].used) continue;
    if (!first) s += ",";
    s += camJson(i);
    first = false;
  }
  UNLOCK();
  return s + "]}";
}

static void sendJson(int code, const String &body) { server.send(code, "application/json", body); }
static void sendErr(int code, const String &msg) { sendJson(code, "{\"ok\":false,\"error\":" + jstr(msg) + "}"); }
static const char *ERRO_TOKEN = "wrong or missing gateway token (set the S3 up again by USB from the Atom)";
// Nos handlers protegidos: sem o token certo responde 403 e sai.
#define PRECISA_TOKEN() do { if (!tokenOk()) { sendErr(403, ERRO_TOKEN); return; } } while (0)
// O Wi-Fi: no modo de configuracao (AP proprio) a pagina muda-o sem token; ligado a rede das camaras, so com ele.
// No F6 autonomo o AP fica sempre no ar: quem esta ligado a ele (o iPhone) nao muda o Wi-Fi do S3 sem o token.
#define PRECISA_TOKEN_WIFI() do { if ((wifiSt != WSTATE_AP || soloF6) && !tokenOk()) { sendErr(403, ERRO_TOKEN); return; } } while (0)
// No F6 autonomo sem token guardado, quem sabe a palavra-passe do Wi-Fi pode tudo o que "sem token" deixa: o que apaga
// dados ou grava firmware exige que haja um token (posto pelo Atom, por USB). O reinicio e o LED continuam abertos.
static const char *ERRO_TOKEN_SOLO = "standalone F6 without a gateway token: set the S3 up by USB from the Atom first";
#define PRECISA_TOKEN_FORTE() do { PRECISA_TOKEN(); if (soloF6 && !gwToken.length()) { sendErr(403, ERRO_TOKEN_SOLO); return; } } while (0)

// ------------------------------------------------------------- operacoes ---
static String opAdd(const String &mac, int type, const String &name, int &code) {
  String m = mac;
  m.toLowerCase();
  if (m.length() != 17 || m[2] != ':') { code = 400; return "mac: aa:bb:cc:dd:ee:ff"; }
  LOCK();
  for (int i = 0; i < MAX_CAMS; i++) if (cams[i].used && cams[i].mac == m) { UNLOCK(); code = 409; return "already configured as " + String(cams[i].id); }
  int slot = -1;
  for (int i = 0; i < MAX_CAMS; i++) if (!cams[i].used) { slot = i; break; }
  if (slot < 0) { UNLOCK(); code = 409; return "no free slot (max 3 GoPro)"; }
  Cam &c = cams[slot];
  NimBLEClient *velho = c.cli;                         // o cliente BLE do slot fica (nao se perde memoria)
  c = Cam();
  c.cli = velho;
  c.used = true;
  snprintf(c.id, sizeof c.id, "gp%d", slot + 1);
  c.mac = m;
  c.type = type;
  c.name = name.length() ? name.substring(0, 20) : String("GoPro ") + String(slot + 1);
  UNLOCK();
  saveCams();
  code = 200;
  return String(c.id);
}

static String opRemove(const String &id, int &code) {
  int i = camIndex(id);
  if (i < 0) { code = 404; return "no camera " + id; }
  Cam &c = cams[i];
  if (c.recording == 1) { code = 409; return String(c.name) + " is recording: stop it first"; }
  if (c.cli && c.cli->isConnected()) c.cli->disconnect();
  NimBLEDevice::deleteBond(NimBLEAddress(std::string(c.mac.c_str()), c.type));
  LOCK();
  c.used = false;
  c.connected = false;
  UNLOCK();
  saveCams();
  code = 200;
  return "";
}

static String opRec(const String &id, bool on, int &code) {
  int i = camIndex(id);
  if (i < 0) { code = 404; return "no camera " + id; }
  Cam &c = cams[i];
  LOCK();
  if (!c.connected) { UNLOCK(); code = 409; return String(c.name) + " is not connected"; }
  if (!c.cmdDone) { UNLOCK(); code = 409; return "another command is still running"; }
  // So poe o comando na fila e responde logo (o servidor HTTP nao fica preso a espera da camera);
  // o resultado vai no estado (cmd.seq / cmd.state / cmd.ok), que o bridge consulta.
  lastSeq = ++c.cmdSeq;
  c.cmdDone = false;
  c.cmdOk = false;
  c.cmdMsg = "";
  c.cmdState = 1;
  c.wantRec = on ? 1 : 0;
  UNLOCK();
  glog("%s rec %d queued (seq %u)", c.id, on, (unsigned)lastSeq);
  code = 200;
  return "";
}

// Espera (so na serie, para diagnostico) que o comando acabe.
static String opRecEsperar(const String &id, bool on, int &code) {
  String r = opRec(id, on, code);
  if (code != 200) return r;
  int i = camIndex(id);
  uint32_t fim = millis() + 12000;
  while (millis() < fim) {
    delay(100);
    LOCK(); bool d = cams[i].cmdDone; UNLOCK();
    if (d) break;
  }
  LOCK(); bool d = cams[i].cmdDone, ok = cams[i].cmdOk; String m = cams[i].cmdMsg; UNLOCK();
  code = d && ok ? 200 : 502;
  return d ? (ok ? "" : m) : "did not finish in time";
}

// -------------------------------------------------------------------- HTTP ---
// ------------------------------------------------------------------- Wi-Fi ---
static void wifiLoad() {
  Preferences wp;
  wp.begin("wifi", true);
  wifiNets = 0;
  for (int i = 0; i < 2; i++) {
    String k = String(i);
    String ss = wp.getString(("s" + k).c_str(), "");
    if (ss.length()) {
      wifiSsid[wifiNets] = ss;
      wifiPsk[wifiNets] = wp.getString(("p" + k).c_str(), "");
      wifiNets++;
    }
  }
  wp.end();
}

static void wifiSaveAll(const String &s1, const String &p1, const String &s2, const String &p2) {
  Preferences wp;
  wp.begin("wifi", false);
  wp.putString("s0", s1);
  wp.putString("p0", p1);
  wp.putString("s1", s2);
  wp.putString("p1", p2);
  wp.end();
}

static void wifiForgetAll() {
  Preferences wp;
  wp.begin("wifi", false);
  wp.clear();
  wp.end();
  WiFi.disconnect(true, true);                         // apaga tambem as credenciais que o proprio Wi-Fi guardou
}

static void wifiBegin(int idx) {
  wifiTryIdx = idx;
  wifiSt = WSTATE_TRY;
  wifiStateMs = millis();
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(wifiSsid[idx].c_str(), wifiPsk[idx].c_str());
  glog("wifi: trying %s", wifiSsid[idx].c_str());
}

static void wifiStartAp() {
  wifiSt = WSTATE_AP;
  wifiStateMs = millis();
  WiFi.mode(WIFI_AP_STA);                              // AP + STA: o portal consegue procurar redes
  WiFi.softAP(apName.c_str());
  glog("wifi: AP %s open at %s", apName.c_str(), WiFi.softAPIP().toString().c_str());
}

// F6 autonomo: o S3 e o ponto de acesso (WPA2) e nao procura redes. O AP fica sempre no ar; quem estiver ligado a ele
// (o iPhone, o iPad) abre http://192.168.4.1/ e comanda o F6.
static void wifiSoloAp() {
  wifiSt = WSTATE_AP;
  wifiStateMs = millis();
  WiFi.mode(WIFI_AP);
  WiFi.softAP(apName.c_str(), soloPsk.c_str(), 6, 0, 4);       // canal 6, visivel, ate 4 aparelhos
  glog("wifi: F6 standalone AP %s (WPA2) at %s", apName.c_str(), WiFi.softAPIP().toString().c_str());
}

static void wifiSetup() {
  uint64_t chip = ESP.getEfuseMac();
  char nm[24];
  snprintf(nm, sizeof nm, soloF6 ? "F6-REMOTE-%02X%02X" : "GOPRO-S3-%02X%02X", (unsigned)((chip >> 32) & 0xFF), (unsigned)((chip >> 40) & 0xFF));
  apName = nm;
  if (soloF6) { wifiSoloAp(); return; }
  wifiLoad();
  if (wifiNets > 0) {
    wifiBegin(0);
  } else {
    // Sem redes guardadas: o firmware anterior deixou a que usava na memoria do Wi-Fi. Tenta-se essa uma vez; se
    // ligar, guarda-se (um gateway actualizado por Wi-Fi nao fica sem rede). Se nao, abre-se o AP.
    wifiMigrar = true;
    wifiSt = WSTATE_TRY;
    wifiStateMs = millis();
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin();
  }
}

static void wifiTick() {
  if (soloF6) return;                                  // o AP fica sempre no ar
  static uint32_t lostMs = 0;
  uint32_t now = millis();
  bool lig = WiFi.status() == WL_CONNECTED;
  if (wifiSt == WSTATE_TRY) {
    if (lig) {
      wifiSt = WSTATE_UP;
      wifiStateMs = now;
      lostMs = 0;
      if (wifiMigrar) {                                // veio das credenciais antigas: ficam guardadas
        wifiMigrar = false;
        wifiSaveAll(WiFi.SSID(), WiFi.psk(), "", "");
        wifiLoad();
        glog("wifi: kept the previous network %s", WiFi.SSID().c_str());
      }
      glog("wifi: up %s", WiFi.localIP().toString().c_str());
    } else if (now - wifiStateMs > WIFI_TRY_MS) {
      if (wifiMigrar) { wifiMigrar = false; wifiStartAp(); }
      else if (wifiTryIdx + 1 < wifiNets) wifiBegin(wifiTryIdx + 1);
      else wifiStartAp();
    }
  } else if (wifiSt == WSTATE_UP) {
    if (lig) { lostMs = 0; return; }
    if (!lostMs) lostMs = now;
    if (now - lostMs > WIFI_LOST_MS) {
      lostMs = 0;
      glog("wifi: lost, trying the saved networks again");
      if (wifiNets > 0) wifiBegin(0); else wifiStartAp();
    }
  } else {                                             // AP
    if (WiFi.softAPgetStationNum() > 0) wifiStateMs = now;      // alguem a configurar: nao se fecha
    if (wifiNets > 0 && now - wifiStateMs > WIFI_AP_MS) wifiBegin(0);
  }
}

static String wifiJson() {
  String s = "{\"ok\":true,\"state\":" + jstr(wifiSt == WSTATE_UP ? "up" : (wifiSt == WSTATE_AP ? "ap" : "trying")) +
             ",\"ap\":" + jstr(apName) + ",\"ssid\":" + jstr(wifiSt == WSTATE_UP ? WiFi.SSID() : String("")) +
             ",\"ip\":" + jstr(wifiSt == WSTATE_UP ? WiFi.localIP().toString() : WiFi.softAPIP().toString()) + ",\"saved\":[";
  for (int i = 0; i < wifiNets; i++) s += (i ? "," : "") + jstr(wifiSsid[i]);
  return s + "]}";
}

// A pagina do portal (o AP de configuracao). O menu "System" (fechado) tem o selector de modo: GoPro, F6 com o Atom ou F6 autonomo
// (com a palavra-passe do Wi-Fi do comando). Funciona so neste AP, sem token, como o Wi-Fi; ligado a rede, o botao fica desligado.
// CUIDADO: nada de apostrofos em texto (HTML ou mensagens) dentro destes R"HTML(...)HTML": o ctags do Arduino confunde-se e mete
// prototipos de "funcoes" (as do JavaScript) no topo do ficheiro, e nao compila.
static const char WIFI_PAGE[] PROGMEM = R"HTML(<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>GoPro gateway</title>
<style>body{font:16px system-ui;margin:0;background:#111;color:#eee}main{max-width:480px;margin:auto;padding:16px}
h1{font-size:20px}.c{background:#1c1c1c;border-radius:10px;padding:12px;margin:12px 0}
input,button{width:100%;box-sizing:border-box;font:inherit;padding:10px;margin:4px 0;border-radius:8px;border:1px solid #444;background:#222;color:#eee}
button{background:#2a6;border:0}button.g{background:#333}.n{padding:8px;border-bottom:1px solid #333;cursor:pointer}.mu{color:#999;font-size:14px}
details.sy{margin:18px 0 6px}details.sy summary{color:#777;font-size:14px;cursor:pointer;list-style:none;text-align:center;padding:8px}
details.sy summary::-webkit-details-marker{display:none}
.sg{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:8px 0}.sg button{background:#262626;border:1px solid #3a3a3a;padding:10px 4px;font-size:14px;line-height:1.2}
.sg button.on{background:#183a2c;border-color:#2a6;color:#7fe0a8}.sg button.cur::after{content:" (now)";font-size:12px;color:#999}
button:disabled{opacity:.4}</style></head><body><main>
<h1>GoPro gateway <small class=mu id=ap></small></h1>
<div class=c><b id=st>...</b><div class=mu id=sv></div></div>
<div class=c><b>Wi-Fi</b><div id=list class=mu></div><button class=g onclick="scan()" id=sb>Scan networks</button>
<input id=s1 placeholder="Network name (SSID)" autocomplete=off><input id=p1 type=password placeholder="Password (empty = open)">
<input id=s2 placeholder="Second network (optional)" autocomplete=off><input id=p2 type=password placeholder="Password">
<button onclick="save()">Save and restart</button><button class=g onclick="forget()">Forget the saved networks</button><div class=mu id=msg></div></div>
<details class=sy><summary>&#8943; System</summary>
<div class=c><b>Mode</b><div class=mu>What this S3 does.</div>
<div class=sg id=modes><button data-m=gopro>GoPro<br>gateway</button><button data-m=f6>F6 with<br>the Atom</button><button data-m=f6solo>F6<br>standalone</button></div>
<div id=solo style=display:none><input id=psk type=password placeholder="Wi-Fi password for the F6 remote (8-63)" autocomplete=off>
<div class=mu>The S3 opens its own network (F6-REMOTE-xxxx) with this password and serves the F6 remote at http://192.168.4.1/. No Atom needed.</div></div>
<button id=go class=g onclick="trocar()" disabled>Switch mode and restart</button><div class=mu id=mh></div><div class=mu id=mm></div></div></details>
<script>
const $=i=>document.getElementById(i);
async function st(){try{const s=await(await fetch('/api/wifi')).json();$('ap').textContent=s.ap;
$('st').textContent=s.state=='up'?'Connected to '+s.ssid+'  '+s.ip:s.state=='ap'?'Setup mode: '+s.ap+'  '+s.ip:'Connecting...';
$('sv').textContent=s.saved.length?'Saved: '+s.saved.join(', '):'No saved network';}catch(e){}}
async function scan(){$('sb').textContent='Scanning...';try{const l=await(await fetch('/api/wifi/scan')).json();if(l.error)$('msg').textContent=l.error;
$('list').innerHTML='';(l.nets||[]).forEach(n=>{const d=document.createElement('div');d.className='n';d.textContent=n.ssid+'  '+n.rssi+' dBm'+(n.open?'  (open)':'');
d.onclick=()=>{$('s1').value=n.ssid;$('p1').focus()};$('list').appendChild(d)});}catch(e){}$('sb').textContent='Scan networks'}
async function save(){const b=new URLSearchParams({s1:$('s1').value,p1:$('p1').value,s2:$('s2').value,p2:$('p2').value});
const r=await fetch('/api/wifi/save',{method:'POST',body:b}).then(x=>x.json()).catch(()=>({ok:false,error:'no answer'}));
$('msg').textContent=r.ok?'Saved. The gateway restarts and joins the network; reconnect to the bridge network.':(r.error||'failed')}
async function forget(){if(!confirm('Forget the saved networks? The gateway opens its own network again.'))return;
const r=await fetch('/api/wifi/forget',{method:'POST'}).then(x=>x.json()).catch(()=>({ok:false}));$('msg').textContent=r.ok?'Forgotten. Restarting in setup mode.':'failed'}
st();setInterval(st,3000);scan();
let mAtual=null,mEsc=null,setup=false;
async function modo(){try{const s=await(await fetch('/api/status')).json();mAtual=s.mode;setup=($('st').textContent.indexOf('Setup mode')==0);
if(mEsc===null)mEsc=mAtual;pintar()}catch(e){}}
function pintar(){document.querySelectorAll('#modes button').forEach(b=>{b.classList.toggle('on',b.dataset.m==mEsc);b.classList.toggle('cur',b.dataset.m==mAtual)});
$('solo').style.display=mEsc=='f6solo'?'block':'none';
const dif=mEsc&&mEsc!=mAtual;$('go').disabled=!dif||!setup;
$('mh').textContent=setup?'Changing the mode restarts the gateway.':'Switching here only works in setup mode (join the network the gateway itself opens). Otherwise use the Atom console: GATEWAYS > S3 > Mode.'}
document.querySelectorAll('#modes button').forEach(b=>b.onclick=()=>{mEsc=b.dataset.m;pintar()});
async function trocar(){const psk=$('psk').value;
if(mEsc=='f6solo'&&(psk.length<8||psk.length>63)){$('mm').textContent='The password of the F6 remote needs 8 to 63 characters.';return}
const nome={gopro:'GoPro gateway',f6:'F6 with the Atom',f6solo:'F6 standalone'}[mEsc];
if(!confirm('Switch this S3 to: '+nome+'?\nIt restarts now.'+(mEsc=='f6solo'?'\nThe network this page is on goes away; join F6-REMOTE-xxxx with the password you typed and open http://192.168.4.1/':'')))return;
$('go').disabled=true;const b=new URLSearchParams({m:mEsc,psk:psk});
const r=await fetch('/api/mode',{method:'POST',body:b}).then(x=>x.json()).catch(()=>({ok:false,error:'no answer'}));
$('mm').textContent=r.ok?'Done. The gateway is restarting.'+(mEsc=='f6solo'?' Join its new network (F6-REMOTE-xxxx) and open http://192.168.4.1/':''):(r.error||'failed');if(!r.ok)$('go').disabled=false}
modo();setInterval(modo,4000);
</script></main></body></html>)HTML";

// A pagina do comando do F6 (modo autonomo, so no http://192.168.4.1/): REC/STOP/PLAY com toque longo, o estado e os medidores
// das faixas (6 + saida L/R). Ver /api/f6/lite. Fonte para testar num browser: a mesma sem o R"HTML(...)HTML".
static const char F6_PAGE[] PROGMEM = R"HTML(<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name=apple-mobile-web-app-capable content=yes><meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=theme-color content=#0b0d10><title>F6 remote</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--fg:#e8eaed;--mu:#8b929c;--ok:#34c759;--wa:#ffb020;--rd:#ff3b30;--cd:#161a20}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.3 system-ui,-apple-system,sans-serif;padding:max(14px,env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) max(14px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left));min-height:100vh;min-height:100dvh;touch-action:manipulation;display:flex;flex-direction:column;gap:12px}
#st{border-radius:14px;padding:16px;text-align:center;font-weight:700;font-size:24px;background:var(--cd);min-height:62px}
#st.rec{background:var(--rd);color:#fff}#st.wa{background:#3a2c08;color:var(--wa)}#st.off{color:var(--mu)}#st.pl{background:#123a26;color:var(--ok)}
#mt{background:var(--cd);border-radius:14px;padding:12px 10px 8px;display:grid;grid-template-columns:repeat(8,1fr);gap:6px;height:min(34vh,260px)}
.ch{display:flex;flex-direction:column;align-items:center;gap:5px;min-width:0}
.bar{flex:1;width:100%;display:flex;flex-direction:column-reverse;gap:2px}
.sg{flex:1;border-radius:2px;background:#222831}
.sg.on{background:var(--ok)}.sg.on.hi{background:var(--wa)}.sg.on.cl{background:var(--rd)}.sg.pk{box-shadow:inset 0 0 0 2px #fff6}
.lb{font-size:13px;color:var(--mu);font-weight:600}.ch.arm .lb{color:var(--rd)}
#bt{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.b{position:relative;overflow:hidden;border:0;border-radius:18px;padding:0;height:104px;color:#fff;font:700 26px system-ui,-apple-system,sans-serif;touch-action:none;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px}
.b small{font:500 13px system-ui;opacity:.75}
.b i{position:absolute;left:0;bottom:0;height:7px;width:0;background:#fff}
#rec{background:#b3261e;grid-column:1/3;height:128px;font-size:34px}#stop{background:#2f3640}#play{background:#1f5f3f}
.b:disabled{opacity:.3}
#ft{color:var(--mu);font-size:13px;text-align:center;display:flex;flex-direction:column;gap:6px}
#mn{align-self:center;background:none;border:1px solid #2a2f37;color:var(--mu);border-radius:999px;width:44px;height:30px;font-size:20px;line-height:1;padding:0}
#sh{position:fixed;inset:0;background:#000b;display:flex;align-items:flex-end;justify-content:center;z-index:9}
#sh[hidden]{display:none}
#sc{width:100%;max-width:520px;background:#161a20;border-radius:20px 20px 0 0;padding:18px 16px max(18px,env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:10px}
#sc h2{margin:0 0 2px;font-size:18px}#sc p{margin:0 0 4px;color:var(--mu);font-size:13px}
.sb{border:0;border-radius:14px;background:#222831;color:var(--fg);text-align:left;padding:14px 16px;font:600 16px system-ui,-apple-system,sans-serif;display:flex;flex-direction:column;gap:3px}
.sb small{font:400 13px system-ui;color:var(--mu)}.sb.arm{background:#5b2a12;color:#ffd7b8}.sb.arm small{color:#f0b890}
.sb.x{background:none;text-align:center;color:var(--mu)}.sb:disabled{opacity:.4}
</style></head><body>
<div id=st class=off>Connecting...</div>
<div id=mt></div>
<div id=bt>
<button class=b id=rec disabled>&#9679; REC<small>hold to start</small><i></i></button>
<button class=b id=stop disabled>&#9632; STOP<small>hold</small><i></i></button>
<button class=b id=play disabled>&#9654; PLAY<small>hold</small><i></i></button>
</div>
<div id=ft><span id=msg>&nbsp;</span><span>Keep the screen on while you use this: Settings &gt; Display &amp; Brightness &gt; Auto-Lock &gt; Never.</span><button id=mn aria-label=System title=System>&#8943;</button></div>
<div id=sh hidden><div id=sc><h2>System</h2><p>This S3 is the F6 remote. Changing the mode restarts it and this Wi-Fi goes away.</p>
<button class=sb id=mAtom><b>Work with the Atom</b><small>F6 mode: the Atom controls the F6 through this S3</small></button>
<button class=sb id=mGopro><b>GoPro gateway</b><small>Back to the GoPro gateway mode</small></button>
<button class="sb x" id=mClose>Close</button></div></div>
<script>
const $=s=>document.querySelector(s),N=8,LAB=['1','2','3','4','5','6','L','R'],IDX=[0,1,2,3,4,5,8,9],MAXV=12;
const pk=new Array(N).fill(0),pkT=new Array(N).fill(0),segs=[],chs=[];
for(let i=0;i<N;i++){const c=document.createElement('div');c.className='ch';c.innerHTML='<div class=bar></div><div class=lb>'+LAB[i]+'</div>';
 const b=c.firstChild,row=[];for(let s=0;s<MAXV;s++){const d=document.createElement('div');d.className='sg';b.appendChild(d);row.push(d)}
 segs.push(row);chs.push(c);$('#mt').appendChild(c)}
let want=null,wantT=0,fails=0,okT=0;
function say(t,ms){$('#msg').textContent=t||' ';if(ms)setTimeout(()=>{if($('#msg').textContent===t)say('')},ms)}
function mmss(ms){const s=Math.floor(ms/1000);return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function meters(d){const now=Date.now();
 for(let i=0;i<N;i++){const v=d?Math.max(0,Math.min(MAXV,d.meters[IDX[i]]|0)):0;
  if(v>=pk[i]){pk[i]=v;pkT[i]=now}else if(now-pkT[i]>1200){pk[i]=Math.max(v,pk[i]-1);pkT[i]=now-900}
  for(let s=0;s<MAXV;s++){const e=segs[i][s];e.className='sg'+(s<v?' on'+(s>=9?' hi':'')+(s===MAXV-1?' cl':''):'')+(s===pk[i]-1&&pk[i]>v?' pk':'')}
  chs[i].classList.toggle('arm',!!d&&i<6&&Array.isArray(d.armed)&&d.armed.indexOf(i+1)>=0)}}
function show(d){const st=$('#st'),on=!!(d&&d.connected);
 ['rec','stop','play'].forEach(k=>$('#'+k).disabled=!on);
 if(!d){st.className='off';st.textContent='No connection to the S3';meters(null);if(want!==null){want=null;say('Lost the connection to the S3: look at the F6',9000)}return}
 if(!on){st.className='wa';st.textContent='F6 not linked';meters(null);return}
 const semPistas=Array.isArray(d.armed)&&d.armed.length===0&&!d.recording;
 $('#rec').disabled=semPistas;
 $('#play').disabled=d.recording===true;      // PLAY a gravar poe uma marca e deixa a faixa em pausa
 if(d.recording){st.className='rec';st.textContent='● REC '+(d.rec_ms!=null?mmss(d.rec_ms):'')}
 else if(d.playing){st.className='pl';st.textContent='▶ PLAYING'}
 else if(semPistas){st.className='wa';st.textContent='No track armed'}
 else{st.className='';st.textContent=d.recording===null?'F6 linked':'Ready'}
 meters(d);
 if(want!==null){if((want==='rec'&&d.recording===true)||(want==='stop'&&d.recording===false)||(want==='play'&&d.playing===true)){want=null;say('OK',1200)}
  else if(Date.now()-wantT>3000){say('The F6 did not confirm '+want.toUpperCase(),6000);want=null}}}
async function poll(){const ac=new AbortController(),to=setTimeout(()=>ac.abort(),1500);
 try{const r=await fetch('/api/f6/lite',{cache:'no-store',signal:ac.signal});const d=await r.json();fails=0;okT=Date.now();show(d)}
 catch(e){if(++fails>2)show(null)}
 clearTimeout(to);setTimeout(poll,250)}
async function fire(k,note){say('...');const ac=new AbortController(),to=setTimeout(()=>ac.abort(),3000);
 try{const r=await fetch('/api/f6/press?note='+note,{method:'POST',signal:ac.signal});const d=await r.json();
  if(!d.ok){say(d.error||('Error '+r.status),6000);return}
  want=k;wantT=Date.now();say('Sent, waiting for the F6 ...');if(navigator.vibrate)navigator.vibrate(50)}
 catch(e){want=null;say(e.name==='AbortError'?'No answer from the S3: look at the F6 before you press again':'Not sent: no connection',9000)}
 clearTimeout(to)}
function hold(id,ms,note){const b=$('#'+id),bar=b.querySelector('i');let t0=0,raf=0;
 const stop=()=>{cancelAnimationFrame(raf);bar.style.width=0;t0=0};
 b.addEventListener('pointerdown',e=>{if(b.disabled)return;e.preventDefault();try{b.setPointerCapture(e.pointerId)}catch(x){}t0=performance.now();
  const step=()=>{if(!t0)return;const p=(performance.now()-t0)/ms;bar.style.width=Math.min(100,p*100)+'%';
   if(p>=1){stop();fire(id,note);return}raf=requestAnimationFrame(step)};step()});
 ['pointerup','pointercancel','lostpointercapture'].forEach(ev=>b.addEventListener(ev,stop));
 b.addEventListener('contextmenu',e=>e.preventDefault())}
hold('rec',700,'24');hold('stop',450,'22');hold('play',700,'23');
function sheet(on){$('#sh').hidden=!on}
$('#mn').onclick=()=>sheet(true);$('#mClose').onclick=()=>sheet(false);$('#sh').onclick=e=>{if(e.target===$('#sh'))sheet(false)};
function modo(id,m){const bt=$('#'+id),tt=bt.querySelector('b'),orig=tt.textContent;let arm=0,tm=0;
 bt.onclick=async()=>{if(!arm){arm=1;bt.classList.add('arm');tt.textContent='Tap again to confirm';clearTimeout(tm);
   tm=setTimeout(()=>{arm=0;bt.classList.remove('arm');tt.textContent=orig},4000);return}
  clearTimeout(tm);arm=0;bt.disabled=true;
  try{const r=await fetch('/api/mode?m='+m,{method:'POST'});const d=await r.json();
   if(d.ok){tt.textContent='Restarting ...';say('Restarting ...');setTimeout(()=>sheet(false),1500)}
   else{bt.disabled=false;bt.classList.remove('arm');tt.textContent=orig;say(d.error||('Error '+r.status),8000);sheet(false)}}
  catch(x){bt.disabled=false;bt.classList.remove('arm');tt.textContent=orig;say('Not sent',4000);sheet(false)}}}
modo('mAtom','f6');modo('mGopro','gopro');
try{if(window.isSecureContext&&navigator.wakeLock)navigator.wakeLock.request('screen').catch(()=>{})}catch(e){}
document.addEventListener('visibilitychange',()=>{try{if(!document.hidden&&window.isSecureContext&&navigator.wakeLock)navigator.wakeLock.request('screen').catch(()=>{})}catch(e){}});
poll();
</script></body></html>)HTML";

static void httpSetup() {
  server.on("/api/status", HTTP_GET, []() { sendJson(200, statusJson()); });
  server.on("/", HTTP_GET, []() {
    // Sem cache: o 192.168.4.1 e o mesmo endereco do portal do S3, do C3 e do AP do Atom, e o Safari ficava com a pagina de outra
    // visita (a lista de redes em vez do comando do F6).
    server.sendHeader("Cache-Control", "no-store, max-age=0");
    server.send_P(200, "text/html; charset=utf-8", soloF6 ? F6_PAGE : WIFI_PAGE);
  });
  server.on("/api/wifi", HTTP_GET, []() { sendJson(200, wifiJson()); });
  server.on("/api/wifi/scan", HTTP_GET, []() {
    PRECISA_TOKEN_WIFI();                              // a procura bloqueia o servidor (e o radio) uns segundos: no AP de configuracao e livre, ligado so com token
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    int n = WiFi.scanNetworks(false, true);
    String out = "{\"ok\":true,\"nets\":[";
    std::vector<String> vistos;
    bool first = true;
    for (int i = 0; i < n; i++) {
      String ss = WiFi.SSID(i);
      if (!ss.length()) continue;
      bool dup = false;
      for (auto &v : vistos) if (v == ss) dup = true;
      if (dup) continue;
      vistos.push_back(ss);
      if (!first) out += ",";
      first = false;
      out += "{\"ssid\":" + jstr(ss) + ",\"rssi\":" + String(WiFi.RSSI(i)) + ",\"open\":" +
             String(WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "true" : "false") + "}";
    }
    WiFi.scanDelete();
    sendJson(200, out + "]}");
  });
  server.on("/api/wifi/save", HTTP_POST, []() {
    PRECISA_TOKEN_WIFI();
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    String s1 = server.arg("s1"), p1 = server.arg("p1"), s2 = server.arg("s2"), p2 = server.arg("p2");
    s1.trim(); s2.trim();
    if (!s1.length() || s1.length() > 32 || s2.length() > 32) { sendErr(400, "network name: 1 to 32 characters"); return; }
    if ((p1.length() && p1.length() < 8) || p1.length() > 63 || (p2.length() && p2.length() < 8) || p2.length() > 63) {
      sendErr(400, "password: 8 to 63 characters, or empty for an open network"); return;
    }
    wifiSaveAll(s1, p1, s2, p2);
    glog("wifi: networks saved (%s%s%s)", s1.c_str(), s2.length() ? ", " : "", s2.c_str());
    rebootAtMs = millis() + 800;
    sendJson(200, "{\"ok\":true,\"note\":\"restarting\"}");
  });
  server.on("/api/wifi/forget", HTTP_POST, []() {
    PRECISA_TOKEN_WIFI();
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    wifiForgetAll();
    glog("wifi: saved networks forgotten");
    rebootAtMs = millis() + 800;
    sendJson(200, "{\"ok\":true}");
  });
  server.on("/api/scan", HTTP_POST, []() {
    SO_GOPRO();
    PRECISA_TOKEN();                                   // a procura ocupa o radio (Wi-Fi e Bluetooth): nao a pedem a qualquer um
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    int s = constrain(server.arg("secs").length() ? server.arg("secs").toInt() : 8, 2, 30);
    LOCK(); found.clear(); scanDone = false; scanReq = s; UNLOCK();  // tudo junto: o GET seguinte nunca le o resultado da procura anterior (done=true velho)
    delay(100);
    sendJson(200, "{\"ok\":true,\"secs\":" + String(s) + "}");
  });
  server.on("/api/scan", HTTP_GET, []() {
    SO_GOPRO();
    LOCK();
    String s = "{\"ok\":true,\"done\":" + String(scanDone && scanReq == 0 ? "true" : "false") + ",\"found\":[";
    for (size_t k = 0; k < found.size(); k++) {
      auto &f = found[k];
      String conf = "";
      for (int i = 0; i < MAX_CAMS; i++) if (cams[i].used && cams[i].mac == f.mac) conf = cams[i].id;
      s += String(k ? "," : "") + "{\"mac\":" + jstr(f.mac) + ",\"name\":" + jstr(f.name) + ",\"type\":" + String(f.type) +
           ",\"rssi\":" + String(f.rssi) + ",\"connectable\":" + String(f.conn ? "true" : "false") + ",\"configured\":" + (conf.length() ? jstr(conf) : String("null")) + "}";
    }
    UNLOCK();
    sendJson(200, s + "]}");
  });
  server.on("/api/add", HTTP_POST, []() {
    SO_GOPRO();
    PRECISA_TOKEN();
    int code;
    String r = opAdd(server.arg("mac"), server.arg("type").length() ? server.arg("type").toInt() : 1, server.arg("name"), code);
    if (code == 200) sendJson(200, "{\"ok\":true,\"id\":" + jstr(r) + "}");
    else sendErr(code, r);
  });
  server.on("/api/remove", HTTP_POST, []() {
    SO_GOPRO();
    PRECISA_TOKEN();
    int code;
    String r = opRemove(server.arg("id"), code);
    if (code == 200) sendJson(200, "{\"ok\":true}");
    else sendErr(code, r);
  });
  server.on("/api/rec", HTTP_POST, []() {
    SO_GOPRO();
    PRECISA_TOKEN();
    int code;
    bool on = server.arg("on") == "1" || server.arg("on") == "true";
    String r = opRec(server.arg("id"), on, code);
    if (code == 200) sendJson(200, "{\"ok\":true,\"queued\":true,\"seq\":" + String((unsigned)lastSeq) + "}");
    else sendErr(code, r);
  });
  server.on("/api/clock_fine", HTTP_POST, []() {
    SO_GOPRO();      // medida fina do relogio (~0,1 s): o resultado aparece em fine.* do estado
    PRECISA_TOKEN();                                   // ocupa a ligacao BLE ate 4 s: nao a pedem a qualquer um, nem a gravar
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    int i = camIndex(server.arg("id"));
    if (i < 0) { sendErr(404, "no such camera"); return; }
    if (!cams[i].connected) { sendErr(409, "camera not connected"); return; }
    LOCK();
    if (!cams[i].fineDone) { UNLOCK(); sendErr(409, "a fine clock measurement is already running"); return; }
    uint32_t seq = ++cams[i].fineSeq;
    cams[i].fineDone = false; cams[i].fineOk = false; cams[i].fineInt = false;
    UNLOCK();
    cams[i].fineReq = true;
    sendJson(200, "{\"ok\":true,\"seq\":" + String(seq) + "}");
  });
  server.on("/api/clock", HTTP_POST, []() {
    SO_GOPRO();            // pede a hora da camara; o resultado aparece no estado (clock.seq sobe)
    PRECISA_TOKEN();
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    int i = camIndex(server.arg("id"));
    if (i < 0) { sendErr(404, "no such camera"); return; }
    if (!cams[i].connected) { sendErr(409, "camera not connected"); return; }
    LOCK(); uint32_t seq = cams[i].clkSeq; UNLOCK();
    cams[i].clkReq = true;
    sendJson(200, "{\"ok\":true,\"seq\":" + String(seq + 1) + "}");
  });
  server.on("/api/led", HTTP_POST, []() {             // t=rgb | simple | off: o tipo de LED desta placa (guarda e aplica logo)
    PRECISA_TOKEN();
    int t = ledTipoDe(server.arg("t"));
    if (t < 0) { sendErr(400, "t: rgb | simple | off"); return; }
    prefs.begin("gw", false);
    prefs.putUChar("led", (uint8_t)t);
    prefs.end();
    ledTipoAplicar((uint8_t)t);
    sendJson(200, "{\"ok\":true,\"led_type\":" + jstr(ledTipoNome((uint8_t)t)) + "}");
  });
  server.on("/api/mode", HTTP_POST, []() {            // m=gopro | f6 | f6solo (&psk= 8 a 63): guarda e reinicia
    String m = server.arg("m");
    // Sem token: sair do modo autonomo (a pagina do comando: a autorizacao e estar ligado ao Wi-Fi com palavra-passe) e mudar
    // o modo pela pagina do AP de configuracao (so quem esta ligado a rede do proprio S3, como a configuracao do Wi-Fi).
    if (!(soloF6 && (m == "f6" || m == "gopro"))) PRECISA_TOKEN_WIFI();
    if (m != "gopro" && m != "f6" && m != "f6solo") { sendErr(400, "m: gopro | f6 | f6solo"); return; }
    uint8_t novo = m == "f6solo" ? 2 : (m == "f6" ? 1 : 0), atual = soloF6 ? 2 : (modoF6 ? 1 : 0);
    String psk = server.arg("psk");
    if (novo == atual) {
      // Ja no F6 autonomo com uma palavra-passe valida e diferente da guardada: troca-se e reinicia (o AP so a le ao arrancar).
      if (novo == 2 && psk.length() >= 8 && psk.length() <= 63 && psk != soloPsk) {
        PRECISA_TOKEN_FORTE();                         // trocar a palavra-passe do AP tranca os outros fora: so com token (e que haja um)
        if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
        modoGuardar(novo, &psk);
        glog("mode: f6solo password changed, rebooting");
        rebootAtMs = millis() + 600;
        sendJson(200, "{\"ok\":true,\"mode\":\"f6solo\",\"note\":\"password changed, restarting\"}");
        return;
      }
      sendJson(200, "{\"ok\":true,\"mode\":" + jstr(m) + ",\"note\":\"already\"}");
      return;
    }
    if (novo == 2 && (psk.length() < 8 || psk.length() > 63)) { sendErr(400, "psk: 8 to 63 characters (the Wi-Fi password of the F6 remote)"); return; }
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    if (novo == 2) modoGuardar(novo, &psk); else modoGuardar(novo);
    glog("mode: %s, rebooting", m.c_str());
    rebootAtMs = millis() + 600;
    sendJson(200, "{\"ok\":true,\"mode\":" + jstr(m) + "}");
  });
  server.on("/api/f6", HTTP_GET, []() {
    if (!modoF6) { sendErr(409, "the S3 is in GoPro mode"); return; }
    sendJson(200, "{\"ok\":true,\"f6\":" + f6Json() + "}");
  });
  server.on("/api/f6/lite", HTTP_GET, []() {         // para a pagina do comando: curto, 4 vezes por segundo
    if (!modoF6) { sendErr(409, "the S3 is in GoPro mode"); return; }
    sendJson(200, f6Lite());
  });
  server.on("/api/f6/press", HTTP_POST, []() {
    if (!soloF6) PRECISA_TOKEN();                      // no autonomo a palavra-passe do Wi-Fi e a autorizacao
    if (!modoF6) { sendErr(409, "the S3 is in GoPro mode"); return; }
    if (!f6Ready()) { sendErr(409, "F6 not connected to the S3"); return; }
    String nota = server.arg("note");
    char *fim = nullptr;
    long n = strtol(nota.c_str(), &fim, 16);
    if (!nota.length() || *fim || n < 0 || n > 0x7F) { sendErr(400, "note: 00 to 7f (hex)"); return; }
    if (soloF6 && n != 0x24 && n != 0x22 && n != 0x23) { sendErr(403, "standalone: REC, STOP and PLAY only"); return; }
    // Autonomo: sem pistas armadas o F6 nao grava, e o REC ficava a espera de uma confirmacao que nunca vem. Recusa-se ja.
    // So quando o F6 ja disse quais estao armadas: sem isso nao se sabe (como no Atom, so "nenhuma" bloqueia).
    if (soloF6 && n == 0x24 && f6Rec != 1 && f6ArmsKnown && f6Arms == 0) { sendErr(409, "no track armed on the F6"); return; }
    // PLAY com o F6 a gravar poe uma marca na take e deixa a faixa em pausa (visto no F6 a 2026-10-18): nunca se envia.
    if (n == F6_NOTE_PLAY && f6Rec == 1) { sendErr(409, "the F6 is recording: PLAY would add a mark and pause the track"); return; }
    f6Press((uint8_t)n);
    if (n == 0x24 || n == 0x22) glog("f6: %s pressed", n == 0x24 ? "REC" : "STOP");
    sendJson(200, "{\"ok\":true}");
  });
  server.on("/api/f6/midi", HTTP_POST, []() {
    PRECISA_TOKEN();
    // No autonomo so passam REC, STOP e PLAY (ver /api/f6/press): o MIDI cru contornava essa lista (menus, faixas, apagar...).
    if (soloF6) { sendErr(403, "standalone: raw MIDI is not allowed (REC, STOP and PLAY only)"); return; }
    if (!modoF6) { sendErr(409, "the S3 is in GoPro mode"); return; }
    if (!f6Ready()) { sendErr(409, "F6 not connected to the S3"); return; }
    String h = server.arg("hex");
    uint8_t m[64];
    int n = 0;
    for (unsigned k = 0; k < h.length() && n < 64; ) {
      while (k < h.length() && !isxdigit((unsigned char)h[k])) k++;
      if (k + 1 >= h.length() || !isxdigit((unsigned char)h[k + 1])) break;
      m[n++] = (uint8_t)strtol(h.substring(k, k + 2).c_str(), nullptr, 16);
      k += 2;
    }
    if (!n || m[0] < 0x80) { sendErr(400, "hex: one MIDI message, e.g. f0 52 ... f7"); return; }
    // Tambem pelo MIDI cru: o PLAY (nota 23, qualquer canal) com o F6 a gravar poe uma marca e deixa a faixa em pausa.
    if (f6Rec == 1 && n >= 3 && (m[0] & 0xF0) == 0x90 && m[1] == F6_NOTE_PLAY && m[2] > 0) {
      sendErr(409, "the F6 is recording: PLAY would add a mark and pause the track"); return;
    }
    f6Send(m, n);
    sendJson(200, "{\"ok\":true,\"bytes\":" + String(n) + "}");
  });
  server.on("/api/log", HTTP_GET, []() {
    String s = "{\"ok\":true,\"lines\":[";
    xSemaphoreTake(mtx, portMAX_DELAY);
    uint32_t n = logCount < LOG_N ? logCount : LOG_N, first = logCount - n;
    for (uint32_t k = 0; k < n; k++) s += String(k ? "," : "") + jstr(logRing[(first + k) % LOG_N]);
    xSemaphoreGive(mtx);
    sendJson(200, s + "]}");
  });
  server.on("/api/reset", HTTP_POST, []() {            // what=reboot | bonds | factory
    PRECISA_TOKEN();
    String w = server.arg("what").length() ? server.arg("what") : String("reboot");
    if (w != "reboot" && soloF6 && !gwToken.length()) { sendErr(403, ERRO_TOKEN_SOLO); return; }   // o reinicio fica aberto
    // O reinicio fica sempre (e a saida de um gateway preso); esquecer emparelhamentos ou tudo, so sem ninguem a gravar.
    if (w != "reboot" && gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    if (w == "reboot") {
      glog("reset: reboot requested");
      rebootAtMs = millis() + 600;
      sendJson(200, "{\"ok\":true,\"what\":\"reboot\"}");
    } else if (modoF6 && w != "reboot") {
      sendErr(409, ERRO_MODO_F6);
    } else if (w == "bonds") {                         // esquece os emparelhamentos; as camaras voltam a ligar-se (pedem emparelhar de novo)
      for (int i = 0; i < MAX_CAMS; i++) if (cams[i].used && cams[i].cli && cams[i].cli->isConnected()) cams[i].cli->disconnect();
      bool ok = NimBLEDevice::deleteAllBonds();
      glog("reset: bonds cleared (%d)", ok);
      sendJson(200, String("{\"ok\":") + (ok ? "true" : "false") + ",\"what\":\"bonds\"}");
    } else if (w == "factory") {                       // esquece tudo: camaras configuradas e emparelhamentos
      for (int i = 0; i < MAX_CAMS; i++) {
        if (cams[i].used && cams[i].cli && cams[i].cli->isConnected()) cams[i].cli->disconnect();
        LOCK(); cams[i].used = false; cams[i].connected = false; UNLOCK();
      }
      NimBLEDevice::deleteAllBonds();
      saveCams();
      glog("reset: factory (all cameras and bonds forgotten)");
      rebootAtMs = millis() + 600;
      sendJson(200, "{\"ok\":true,\"what\":\"factory\"}");
    } else {
      sendErr(400, "what: reboot | bonds | factory");
    }
  });
  // OTA: o bridge manda o .bin; vai para a outra particao e so passa a arrancar se chegou inteiro e o MD5 bate.
  server.on("/api/fw", HTTP_POST, []() {
    // A funcao de envio (abaixo) so corre se vier um ficheiro multipart: um POST sem ficheiro chega aqui sem ter passado
    // por ela. Por isso o token e a gravacao confirmam-se tambem aqui, e so se reinicia com um envio concluido (otaConcluido).
    int cod = 0;
    String erro;
    if (!tokenOk()) { cod = 403; erro = ERRO_TOKEN; }
    else if (soloF6 && !gwToken.length()) { cod = 403; erro = ERRO_TOKEN_SOLO; }
    else if (otaRecusado) { cod = otaCodigo; erro = otaErr; }
    else if (otaErr.length() || Update.hasError()) { cod = 500; erro = otaErr.length() ? otaErr : String(Update.errorString()); }
    else if (!otaConcluido) { cod = 400; erro = "no firmware received (multipart field fw, with md5)"; }
    // Ja gravado e confirmado (Update.end): o proximo arranque, seja qual for, corre o novo. Nao e um erro: avisa-se que fica pendente.
    if (!cod && gravaAlgo()) {
      glog("ota: written, a camera is recording: restarts at the next boot");
      sendJson(200, "{\"ok\":true,\"pending_restart\":true,\"note\":\"written; restarts at the next boot\"}");
    } else if (!cod) {
      glog("ota: ok, rebooting");
      rebootAtMs = millis() + 800;
      sendJson(200, "{\"ok\":true}");
    } else {
      sendErr(cod, erro);
    }
    otaErr = "";
    otaRecusado = false;
    otaConcluido = false;
  }, []() {
    HTTPUpload &u = server.upload();
    if (u.status == UPLOAD_FILE_START) {
      otaErr = "";
      otaRecusado = false;
      otaConcluido = false;
      if (!tokenOk()) { otaRecusado = true; otaCodigo = 403; otaErr = ERRO_TOKEN; return; }
      if (soloF6 && !gwToken.length()) { otaRecusado = true; otaCodigo = 403; otaErr = ERRO_TOKEN_SOLO; return; }
      if (gravaAlgo()) { otaRecusado = true; otaCodigo = 409; otaErr = modoF6 ? "the F6 is recording" : "a GoPro is recording"; return; }
      if (!Update.begin(UPDATE_SIZE_UNKNOWN)) { otaErr = Update.errorString(); return; }
      String md5 = server.arg("md5");
      if (md5.length() != 32) { otaErr = "md5 missing"; Update.abort(); return; }    // sem MD5 nao se grava
      Update.setMD5(md5.c_str());
      glog("ota: receiving %s", u.filename.c_str());
    } else if (u.status == UPLOAD_FILE_WRITE) {
      esp_task_wdt_reset();                            // um envio lento nao pode cair no vigia de 30 s a meio da gravacao
      if (otaErr.length()) return;
      if (Update.write(u.buf, u.currentSize) != u.currentSize) otaErr = Update.errorString();
    } else if (u.status == UPLOAD_FILE_END) {
      if (otaErr.length()) { Update.abort(); return; }
      if (!Update.end(true)) otaErr = Update.errorString();
      else { otaConcluido = true; glog("ota: %u bytes written", (unsigned)u.totalSize); }
    } else if (u.status == UPLOAD_FILE_ABORTED) {
      Update.abort();
      otaErr = "upload aborted";
    }
  });
  server.on("/api/fw/rollback", HTTP_POST, []() {
    PRECISA_TOKEN_FORTE();                             // grava firmware: no F6 autonomo sem token guardado ninguem o faz pelo Wi-Fi
    if (!Update.canRollBack()) { sendErr(409, "no previous firmware"); return; }
    if (gravaAlgo()) { sendErr(409, modoF6 ? "the F6 is recording" : "a GoPro is recording"); return; }
    if (!Update.rollBack()) { sendErr(500, "rollback failed"); return; }
    glog("ota: rollback, rebooting");
    rebootAtMs = millis() + 600;
    sendJson(200, "{\"ok\":true}");
  });
  server.on("/api/clear_bonds", HTTP_POST, []() {
    SO_GOPRO();
    PRECISA_TOKEN();
    if (gravaAlgo()) { sendErr(409, "recording: stop first"); return; }
    sendJson(200, String("{\"ok\":") + (NimBLEDevice::deleteAllBonds() ? "true" : "false") + "}");
  });
  server.onNotFound([]() { sendErr(404, "no such route"); });
  static const char *cabecalhos[] = {"X-GW-Token"};
  server.collectHeaders(cabecalhos, 1);
  server.begin();
}

// ------------------------------------------------------------------- serie ---
// WIFISET / WIFISETX: o Wi-Fi guardado pela USB. p[0..3] = ssid, psk, ssid2, psk2 (ja decodificados e, no WIFISET, sem
// espacos nas pontas do nome). Responde na serie e reinicia se ficou guardado.
static void wifiSetSerie(String p[4]) {
  if (!p[0].length() || p[0].length() > 32 || p[2].length() > 32 || p[1].length() > 63 || p[3].length() > 63 ||
      (p[1].length() && p[1].length() < 8) || (p[3].length() && p[3].length() < 8)) {
    Serial.println("ERR WIFISET ssid|psk[|ssid2|psk2]: ssid 1-32, password empty or 8-63");
  } else {
    wifiSaveAll(p[0], p[1], p[2], p[3]);
    Serial.println("OK wifi saved, rebooting");
    rebootAtMs = millis() + 600;
  }
}

static int hexNib(char ch) {
  if (ch >= '0' && ch <= '9') return ch - '0';
  if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
  if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
  return -1;
}

// Hex -> texto (bytes tal e qual, sem cortar espacos). false se nao for hex de comprimento par.
static bool hexParaTexto(const String &h, String &out) {
  out = "";
  if (h.length() % 2) return false;
  for (size_t k = 0; k < h.length(); k += 2) {
    int a = hexNib(h[k]), b = hexNib(h[k + 1]);
    if (a < 0 || b < 0) return false;
    char ch = (char)((a << 4) | b);
    if (!ch) return false;
    out += ch;
  }
  return true;
}

static void serialCmd(String l) {
  String crua = l;                       // sem cortar as pontas: a password do WIFISET pode ter espacos no fim ou no inicio
  l.trim();
  if (!l.length()) return;
  int sp = l.indexOf(' ');
  String c = sp < 0 ? l : l.substring(0, sp);
  String rest = sp < 0 ? "" : l.substring(sp + 1);
  c.toUpperCase();
  int code = 0;
  if (modoF6 && (c == "SCAN" || c == "FOUND" || c == "ADD" || c == "DEL" || c == "REC" || c == "RAW" || c == "RAWQ" || c == "FINE")) {
    Serial.printf("ERR %s\n", ERRO_MODO_F6);
    return;
  }
  if (c == "STATUS") Serial.println(statusJson());
  else if (c == "SCAN") {
    if (gravaAlgo()) { Serial.println("ERR recording: stop first"); return; }
    LOCK(); found.clear(); scanDone = false; scanReq = rest.length() ? rest.toInt() : 8; UNLOCK();
    Serial.println("OK scanning");
  }
  else if (c == "FOUND") {
    LOCK();
    for (auto &f : found) Serial.printf("FOUND %s type=%u rssi=%d conn=%d name=%s\n", f.mac.c_str(), f.type, f.rssi, f.conn, f.name.c_str());
    Serial.printf("OK done=%d\n", scanDone);
    UNLOCK();
  } else if (c == "ADD") {
    int a = rest.indexOf(' '), b = rest.indexOf(' ', a + 1);
    String r = opAdd(rest.substring(0, a), rest.substring(a + 1, b).toInt(), rest.substring(b + 1), code);
    Serial.printf("%s %s\n", code == 200 ? "OK" : "ERR", r.c_str());
  } else if (c == "DEL") { String r = opRemove(rest, code); Serial.printf("%s %s\n", code == 200 ? "OK" : "ERR", r.c_str()); }
  else if (c == "REC") {
    int a = rest.indexOf(' ');
    String r = opRecEsperar(rest.substring(0, a), rest.substring(a + 1).toInt() != 0, code);
    Serial.printf("%s %s\n", code == 200 ? "OK" : "ERR", r.c_str());
  } else if (c == "RAW" || c == "RAWQ") {                     // diagnostico: RAW gp1 hex  (escreve na GP-0072; a resposta vem em "cmd response")
    int a = rest.indexOf(' ');
    int i = camIndex(rest.substring(0, a));
    String hx = rest.substring(a + 1);
    std::vector<uint8_t> d;
    for (size_t k = 0; i >= 0 && k + 1 < hx.length(); k += 2) d.push_back((uint8_t)strtol(hx.substring(k, k + 2).c_str(), nullptr, 16));
    bool q = c == "RAWQ";
    if (i < 0 || !cams[i].connected || !cams[i].chCmd) Serial.println("ERR no such connected camera");
    else Serial.println((q ? cams[i].chQry : cams[i].chCmd)->writeValue(d.data(), d.size(), true) ? "OK written" : "ERR write failed");
  } else if (c == "FINE") {                    // diagnostico: FINE gp1 -> mede a fase do relogio da camara
    int i = camIndex(rest);
    if (i < 0 || !cams[i].connected) { Serial.println("ERR no such connected camera"); return; }
    LOCK(); cams[i].fineSeq++; cams[i].fineDone = false; cams[i].fineOk = false; UNLOCK();
    uint32_t t0 = millis();
    cams[i].fineReq = true;
    while (millis() - t0 < 6000) { LOCK(); bool d = cams[i].fineDone; UNLOCK(); if (d) break; delay(20); }
    LOCK();
    Serial.printf("FINE ok=%d lo=%u hi=%u gap=%u ms  camera %04u-%02u-%02u %02u:%02u:%02u  gw_now=%u\n", cams[i].fineOk, (unsigned)cams[i].fineLo,
                  (unsigned)cams[i].fineHi, (unsigned)(cams[i].fineHi - cams[i].fineLo), cams[i].fnY, cams[i].fnMo, cams[i].fnD, cams[i].fnH,
                  cams[i].fnMi, cams[i].fnS, (unsigned)millis());
    UNLOCK();
  } else if (c == "WIFI") { Serial.println(wifiJson());
  } else if (c == "WIFISET") {                    // WIFISET ssid|psk[|ssid2|psk2]: o Atom configura o Wi-Fi por USB
    if (gravaAlgo()) { Serial.println("ERR recording: stop first"); return; }
    String p[4];
    int n = 0, from = 0;
    int ini = 0;
    while (ini < (int)crua.length() && isspace((unsigned char)crua[ini])) ini++;      // salta os espacos antes do comando
    int sp0 = crua.indexOf(' ', ini);
    rest = sp0 < 0 ? String("") : crua.substring(sp0 + 1);                            // o resto tal e qual (so sem \r\n, que a leitura da serie ja tira)
    while (n < 4) {
      int bar = rest.indexOf('|', from);
      if (bar < 0) { p[n++] = rest.substring(from); break; }
      p[n++] = rest.substring(from, bar);
      from = bar + 1;
    }
    p[0].trim(); p[2].trim();
    wifiSetSerie(p);
  } else if (c == "WIFISETX") {                   // WIFISETX hexssid|hexpsk[|hexssid2|hexpsk2]: o mesmo em hex (nomes e passwords com espacos nas pontas)
    if (gravaAlgo()) { Serial.println("ERR recording: stop first"); return; }
    String p[4], h[4];
    int n = 0, from = 0;
    while (n < 4) {
      int bar = rest.indexOf('|', from);
      if (bar < 0) { h[n++] = rest.substring(from); break; }
      h[n++] = rest.substring(from, bar);
      from = bar + 1;
    }
    bool ok = true;
    for (int k = 0; k < 4 && ok; k++) { h[k].trim(); ok = hexParaTexto(h[k], p[k]); }
    if (!ok) Serial.println("ERR WIFISET hex: ssid|psk[|ssid2|psk2], each in hexadecimal");
    else wifiSetSerie(p);
  } else if (c == "TOKENSET") {                   // TOKENSET <hex>: o Atom grava o token pela USB (so por aqui)
    rest.trim();
    bool hex = rest.length() >= 16 && rest.length() <= 64;
    for (size_t k = 0; hex && k < rest.length(); k++) hex = isxdigit((unsigned char)rest[k]);
    if (!hex) { Serial.println("ERR TOKENSET <16 to 64 hex characters>"); return; }
    Preferences tp;
    tp.begin("sec", false);
    tp.putString("tok", rest);
    tp.end();
    gwToken = rest;
    Serial.println("OK token saved");            // o token em si nunca vai para a serie nem para o registo
  } else if (c == "LED") {                       // LED rgb|simple|off: o tipo de LED da placa (StampS3 = rgb, XIAO = simple)
    int t = ledTipoDe(rest);
    if (t < 0) { Serial.printf("OK led %s\n", ledTipoNome(ledTipo)); return; }
    prefs.begin("gw", false);
    prefs.putUChar("led", (uint8_t)t);
    prefs.end();
    ledTipoAplicar((uint8_t)t);
    Serial.printf("OK led %s\n", ledTipoNome((uint8_t)t));
  } else if (c == "MODE") {                      // MODE gopro|f6 (a configuracao por USB, com o S3 ligado ao Atom)
    rest.trim();
    rest.toLowerCase();
    if (rest != "gopro" && rest != "f6") { Serial.printf("OK mode %s\n", soloF6 ? "f6solo" : modoF6 ? "f6" : "gopro"); return; }
    // O modo real tem tres valores (0 GoPro, 1 F6 com o Atom, 2 F6 autonomo): MODE f6 ou gopro a partir do autonomo sai dele
    // (antes `MODE f6` no autonomo achava que ja estava em f6 e nao fazia nada).
    uint8_t novo = rest == "f6" ? 1 : 0, atual = soloF6 ? 2 : (modoF6 ? 1 : 0);
    if (novo == atual) { Serial.printf("OK mode %s\n", rest.c_str()); return; }
    if (gravaAlgo()) { Serial.println("ERR recording: stop first"); return; }
    modoGuardar(novo);
    Serial.printf("OK mode %s, rebooting\n", rest.c_str());
    rebootAtMs = millis() + 300;
  } else if (c == "WIFIFORGET") {
    if (gravaAlgo()) { Serial.println("ERR recording: stop first"); return; }
    wifiForgetAll(); Serial.println("OK forgotten, rebooting"); rebootAtMs = millis() + 300;
  } else if (c == "REBOOT") { Serial.println("OK rebooting"); rebootAtMs = millis() + 300; }
  else if (c == "CLEAR") Serial.println(!gravaAlgo() && !modoF6 && NimBLEDevice::deleteAllBonds() ? "OK" : "ERR");
  else Serial.println("ERR STATUS | SCAN [s] | FOUND | ADD mac type name | DEL id | REC id 0|1 | CLEAR | WIFI | WIFISET ssid|psk | WIFISETX hex|hex | WIFIFORGET | TOKENSET hex | MODE gopro|f6");
}

// ---------------------------------------------------------------- LED de estado ---
// Azul: arranque (LED_BOOT_MS). Verde: >=1 GoPro ligada e nenhuma das que ja tinham estado ligadas esta em falta.
// Amarelo a piscar: nenhuma GoPro ligada, ou alguma que ja tinha estado ligada caiu.
// Vermelho: nunca (a falta de Wi-Fi nao entra aqui; o bridge ja a mostra no estado do gateway).
static void ledSet(uint8_t r, uint8_t g, uint8_t b) {
  if (ledTipo == 1) neopixelWrite(LED_PIN, r, g, b);
  else if (ledTipo == 2) digitalWrite(LED_PIN, (r | g | b) ? LOW : HIGH);      // LED simples: acende a 0
}

// Chamada no loop(): so escreve no LED quando o que se ve muda (o amarelo pisca de meio em meio segundo).
static void ledUpdate() {
  static const char *antes = "";          // estado mostrado na vez anterior
  static bool antesLuz = false;           // amarelo aceso na vez anterior
  uint32_t now = millis();
  int ligadas = 0, emFalta = 0;
  LOCK();
  for (int i = 0; i < MAX_CAMS; i++) {
    if (!cams[i].used) continue;
    if (cams[i].connected) ligadas++;
    else if (cams[i].everUp) emFalta++;
  }
  UNLOCK();
  const char *est = (wifiSt == WSTATE_AP && !soloF6) ? "ap"            // modo de configuracao do Wi-Fi: magenta
                  : modoF6 ? (f6Ready() ? "f6" : (now < LED_BOOT_MS ? "boot" : "miss"))
                  : now < LED_BOOT_MS ? "boot"
                  : (ligadas == 0 || emFalta > 0) ? "miss"      // 0 ligadas (ou nenhuma configurada), ou alguma caiu
                  : "ok";
  ledEstado = est;
  if (ledForcar) { antes = ""; ledForcar = false; }
  bool mudou = strcmp(antes, est) != 0;
  antes = est;
  if (est[0] == 'a') {                    // magenta: AP de configuracao aberto
    if (mudou) ledSet(LED_BRIGHT, 0, LED_BRIGHT);
  } else if (est[0] == 'f') {             // ciano: modo F6 com o F6 ligado
    if (mudou) ledSet(0, LED_BRIGHT, LED_BRIGHT);
  } else if (est[0] == 'b') {             // azul
    if (mudou) ledSet(0, 0, LED_BRIGHT);
  } else if (est[0] == 'o') {             // verde
    if (mudou) ledSet(0, LED_BRIGHT, 0);
  } else {                                // amarelo a piscar
    bool luz = ((now / LED_BLINK_MS) & 1) == 0;
    if (mudou || luz != antesLuz) ledSet(luz ? LED_BRIGHT : 0, luz ? LED_BRIGHT : 0, 0);
    antesLuz = luz;
  }
}

// ----------------------------------------------------------------- arranque ---
// OTA: o firmware novo so fica valido depois de provar que funciona (Wi-Fi ligado e 60 s a correr, ver loop). Se
// rebentar antes, o arranque seguinte volta ao anterior (quando o bootloader tem a reposicao ligada).
bool verifyRollbackLater() { return true; }

static const uint32_t LOOP_WDT_MS = 30000;            // o ciclo principal encravado mais do que isto reinicia o gateway

static bool redeArrancada = false;

// Wi-Fi, HTTP e o reset por UDP (no modo F6 so depois de o F6 estar ligado, ver o loop).
static void redeArrancar() {
  redeArrancada = true;
  if (modoF6) WiFi.setTxPower(WIFI_POWER_11dBm);     // menos pico de corrente na USB do F6 (o AP do Atom esta perto)
  wifiSetup();
  if (modoF6) WiFi.setTxPower(WIFI_POWER_11dBm);
  httpSetup();
  udpRx.begin(PORT_RESET);
}

void setup() {
  Serial.begin(115200);
  mtx = xSemaphoreCreateMutex();
  prefs.begin("gw", true);
  uint8_t md = prefs.getUChar("mode", 0);
  modoF6 = md >= 1;
  soloPsk = prefs.getString("solopsk", "");
  soloF6 = md == 2 && soloPsk.length() >= 8;           // sem palavra-passe valida nao ha AP aberto: volta ao modo F6 normal
  ledTipo = prefs.getUChar("led", 1);
  if (ledTipo > 2) ledTipo = 1;
  prefs.end();
  bool serieAgora = f6SerieUmaVez == F6_SERIE_MAGIC;
  f6SerieUmaVez = 0;
  if (modoF6 && !serieAgora) {
    // O USB host arranca JA, antes de tudo o resto: o F6 corta a alimentacao se nao for atendido em ~1-2 s (com a
    // espera de 400 ms para ver se havia um PC na USB, o S3 apagava-se como antes; 2026-10-15).
    f6Start();
  } else {
    delay(500);
  }
  ledTipoAplicar(ledTipo);                 // StampS3: liga a alimentacao do LED RGB; XIAO: o LED simples
  loadCams();
  tokenLoad();
  glog("mode %s%s", modoF6 ? "F6" : "GoPro", modoF6 && !f6Host ? " (plugged into a computer: USB serial, no F6 link)" : "");
  if (!modoF6) {
    NimBLEDevice::init("gopro-gw");
    NimBLEDevice::setSecurityAuth(true, false, true);
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
    xTaskCreate(bleTask, "ble", 8192, nullptr, 1, nullptr);
    redeArrancar();
  }
  bleTickMs = millis();
  // Vigia do proprio ciclo (antes so a tarefa BLE era vigiada): 30 s, com reinicio.
  esp_task_wdt_config_t wdt = {LOOP_WDT_MS, 0, true};
  if (esp_task_wdt_reconfigure(&wdt) != ESP_OK) esp_task_wdt_init(&wdt);
  esp_task_wdt_add(NULL);
  if (!f6Host) Serial.println("OK gopro_gw ready");
}

void loop() {
  static uint32_t beaconMs = 0, wifiLogMs = 0;
  static bool otaValido = false;
  esp_task_wdt_reset();
  if (f6Host && !f6BusVisto && millis() > F6_SEM_BUS_MS) {
    glog("f6: nothing on the USB in %d s (plugged into the Atom?): restarting once with the USB serial port", F6_SEM_BUS_MS / 1000);
    f6SerieUmaVez = F6_SERIE_MAGIC;
    delay(50);
    ESP.restart();
  }
  if (!redeArrancada) {
    if (!f6Host || f6ArmsKnown || (f6Ready() && millis() - f6OpenMs > 3000) || millis() > 8000) redeArrancar();
    else { ledUpdate(); delay(5); return; }
  }
  server.handleClient();
  while (!f6Host && Serial.available()) {
    static String linha;
    static bool longa = false;                       // a linha passou do limite: deita-se fora inteira (nunca se executa cortada)
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (longa) { Serial.println("ERR line too long"); longa = false; }
      else serialCmd(linha);
      linha = "";
    }
    else if (linha.length() < 520) linha += ch;      // WIFISETX com duas redes: 9 + 2*(64+126) + 3 = 392 no maximo
    else longa = true;
  }
  uint32_t now = millis();
  wifiTick();
  ledUpdate();
  if (rebootAtMs && now > rebootAtMs) { glog("rebooting"); delay(100); ESP.restart(); }
  int pk = udpRx.parsePacket();                        // reinicio por UDP: funciona mesmo com o servidor HTTP preso
  if (pk > 0) {
    char b[100] = {0};
    udpRx.read(b, sizeof b - 1);
    if (strncmp(b, "GPGW-RESET", 10) == 0) {
      String tok = String(b + 10);                     // "GPGW-RESET <token>"
      tok.trim();
      if (tokenIgual(tok)) { glog("reset: UDP reset received"); rebootAtMs = now + 300; }
      else glog("reset: UDP reset with a wrong token ignored");
    }
  }
  // Vigias: a tarefa BLE presa mais de 60 s, ou o Wi-Fi em baixo mais de 10 min, reiniciam o gateway.
  static uint32_t wifiDownSince = 0;
  if (WiFi.status() == WL_CONNECTED || wifiSt == WSTATE_AP) wifiDownSince = 0; else if (!wifiDownSince) wifiDownSince = now;
  // Com um millis() fresco e a diferenca com sinal: o bleTickMs pode ter mudado depois de se ler o `now` (a conta sem
  // sinal dava ~4e9 e reiniciava o gateway a meio de um take).
  if (!modoF6 && (int32_t)(millis() - bleTickMs) > 60000) { Serial.println("LOG watchdog: BLE task stuck, rebooting"); delay(100); ESP.restart(); }
  if (!otaValido && (WiFi.status() == WL_CONNECTED || soloF6) && millis() > 60000) {
    otaValido = true;
    esp_ota_mark_app_valid_cancel_rollback();            // este firmware funciona: fica (sem efeito se ja era valido)
  }
  if (wifiDownSince && now - wifiDownSince > 600000) { Serial.println("LOG watchdog: Wi-Fi down 10 min, rebooting"); delay(100); ESP.restart(); }
  if (WiFi.status() == WL_CONNECTED && now - beaconMs > 5000) {
    beaconMs = now;
    int n = 0;
    for (auto &c : cams) if (c.used) n++;
    IPAddress bc = WiFi.localIP();
    bc[3] = 255;
    udp.beginPacket(bc, PORT_BEACON);
    udp.printf("GPGW ip=%s cams=%d mode=%s", WiFi.localIP().toString().c_str(), modoF6 ? 0 : n, modoF6 ? "f6" : "gopro");
    udp.endPacket();
  }
  if (!f6Host && now - wifiLogMs > 15000) {
    wifiLogMs = now;
    Serial.printf("LOG wifi %s %s\n", wifiSt == WSTATE_AP ? "AP" : (WiFi.status() == WL_CONNECTED ? "up" : "down"),
                  wifiSt == WSTATE_AP ? WiFi.softAPIP().toString().c_str() : WiFi.localIP().toString().c_str());
  }
  delay(2);
}
