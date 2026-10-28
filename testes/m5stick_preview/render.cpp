// Desenha as paginas do comando de bolso (o codigo do .ino, com a biblioteca de ecra verdadeira) em ficheiros PPM, no PC. Ver LEIA-ME.txt.
#include <lgfx/v1/LGFX_Sprite.hpp>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <algorithm>
using std::min; using std::max;
typedef lgfx::v1::LGFX_Sprite M5Canvas;
typedef int SemaphoreHandle_t;
#define portMAX_DELAY 0
static void xSemaphoreTake(int, int) {}
static void xSemaphoreGive(int) {}
static uint32_t g_now = 100000;
static uint32_t millis() { return g_now; }
enum wl_status_t { WL_IDLE_STATUS, WL_NO_SSID_AVAIL, WL_CONNECTED, WL_CONNECT_FAILED, WL_DISCONNECTED };
struct IPA { std::string toString() const { return "192.168.4.2"; } };
static struct { wl_status_t st = WL_CONNECTED; int rssi = -62; int ch = 6;
  wl_status_t status() { return st; } int RSSI() { return rssi; } int channel() { return ch; } IPA localIP() { return IPA(); } } WiFi;
namespace m5 { struct Power_Class { enum is_charging_t { is_discharging, is_charging, charge_unknown }; }; }
static int g_bat = 87; static bool g_chg = false;
static struct { int getBatteryLevel() { return g_bat; } int16_t getBatteryVoltage() { return 4000; } m5::Power_Class::is_charging_t isCharging() { return g_chg ? m5::Power_Class::is_charging : m5::Power_Class::is_discharging; } } PowerShim;
#define M5Power PowerShim
struct M5Shim { decltype(PowerShim) &Power = PowerShim; } M5;
#define constrain(x, a, b) ((x) < (a) ? (a) : (x) > (b) ? (b) : (x))
#include "render_src.inc"
static int imgN = 0; static const char *tag = "x";
static void host_present() {
  char fn[64]; snprintf(fn, sizeof fn, "shot_%02d_%s.ppm", imgN++, tag);
  FILE *f = fopen(fn, "wb"); fprintf(f, "P6\n240 135\n255\n");
  for (int y = 0; y < 135; y++) for (int x = 0; x < 240; x++) { auto c = cv.readPixelRGB(x, y); unsigned char p[3] = {c.R8(), c.G8(), c.B8()}; fwrite(p, 1, 3, f); }
  fclose(f);
}
static void base() {
  memset(&st, 0, sizeof st);
  st.okMs = g_now - 200; st.connected = 1; st.recording = 0; st.recMs = -1; st.armedKnown = 1; st.armed = 0x3F; st.rtt = 38;
  WiFi.st = WL_CONNECTED; WiFi.rssi = -62; g_bat = 87; g_chg = false; viewMode = 0; unlockUntil = 0; holdAct = -1; taps = 0;
  cmdPhase = C_IDLE; wasRec = false; flashUntil = 0; batMs = 0; memset(peaks, 0, sizeof peaks);
  snprintf(cfg.ssid, sizeof cfg.ssid, "F6-REMOTE-1A2B"); snprintf(cfg.host, sizeof cfg.host, "192.168.4.1");
  uint8_t m[10] = {9, 12, 4, 0, 7, 2, 0, 0, 6, 8}; memcpy(st.meters, m, 10);
}
int main() {
  cv.setColorDepth(16); cv.createSprite(240, 135);
  M5Canvas &c = cv; (void)c;
  tag = "1_rec_pistas"; base(); st.recording = 1; st.recMs = 65432; st.armed = 0x13; draw();
  tag = "2_ready"; base(); draw();
  tag = "3_no_track"; base(); st.armed = 0; draw();
  tag = "4_wifi_ok"; base(); viewMode = 1; draw();
  tag = "5_wifi_fraco"; base(); viewMode = 1; WiFi.rssi = -82; draw();
  tag = "6_wifi_sem"; base(); viewMode = 1; WiFi.st = WL_NO_SSID_AVAIL; st.okMs = 0; draw();
  tag = "7_sistema"; base(); viewMode = 2; g_chg = true; draw();
  tag = "8_sistema_baixa"; base(); viewMode = 2; g_bat = 12; draw();
  tag = "9_desbloqueado"; base(); unlockUntil = g_now + 7000; holdAct = 1; holdT0 = g_now - 300; draw();
  tag = "10_unknown"; base(); st.okMs = g_now - 4000; wasRec = true; g_now += 400; draw(); tag = "10b_unknown_toques"; taps = 2; draw(); tag = "10c_no_link"; base(); st.okMs = g_now - 5000; draw(); tag = "10d_nowifi_p2"; base(); viewMode = 1; WiFi.st = WL_CONNECT_FAILED; st.okMs = 0; draw();
  tag = "11_ssid_longo"; base(); viewMode = 1; snprintf(cfg.ssid, sizeof cfg.ssid, "Casa do Joao e da Maria 2.4G"); draw();
  return 0;
}
