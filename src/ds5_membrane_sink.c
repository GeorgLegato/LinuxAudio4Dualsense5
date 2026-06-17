// ds5_membrane_sink.c — nativer PipeWire-Sink fuer den DualSense-Membran-Speaker.
//
// Registriert ein Audio-Ausgabegeraet "DualSense BT Speaker". PipeWire liefert
// die PCM-Buffers ueber den Graph-Scheduler (praezises Timing). Wir akkumulieren
// 512-Frame-Bloecke, resampeln auf 480, Opus-encoden (CBR 160kbps) und schreiben
// Report 0x36 ans hidraw des Controllers.
//
// Reverse-engineered aus DS5_Bridge (AGPL v3) + SAxense (MPL 2.0). Siehe README.
//
// Build:  make   (oder siehe Makefile)

#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#include <spa/utils/result.h>
#include <opus.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdint.h>
#include <glob.h>
#include <zlib.h>
#include <math.h>
#include <time.h>
#include <sys/stat.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <poll.h>
#include <errno.h>

#define SRC_RATE        48000
#define CHANNELS        2          // Opus an den Controller: stereo (DS5_Bridge)
#define SINK_CH         2          // PipeWire-Sink: 2 Kanaele = FL Membran / FR Haptik
#define CH_MEMBRANE     0          // FL -> Membran-Speaker
#define CH_HAPTIC       1          // FR -> Haptik (Subwoofer-Kette)
#define INPUT_BLOCK     512        // Frames pro Sende-Block (DS5_Bridge)
#define OPUS_FRAME      480        // Opus-Frame nach Resampling
#define OPUS_BITRATE    (200*8*100) // 160 kbps CBR -> exakt 200 byte/frame
#define SPEAKER_BYTES   200
#define REPORT_SIZE     398
#define STATE_SNAP_SIZE 63
#define FADE_SAMPLES    1920
#define PREROLL_PKTS    24
#define HAPTIC_N        64         // 0x12 sub-packet: 64 int8 samples @ 6 kHz
#define HAPTIC_DECIM    (INPUT_BLOCK / HAPTIC_N)  // 512/64 = 8  (48k -> 6k)
#define SUB_PI          3.14159265358979323846
#define CFG_CHECK_PKTS  96         // ~1s: Config-Datei auf Aenderung pruefen

// --- Web/API + Analyser -------------------------------------------------------
#define WEB_PORT_DEFAULT 8118
#define WEB_MAX_CLIENTS  8
#define FFT_N            512        // = INPUT_BLOCK (eine FFT pro Block)
#define NBANDS           28         // Spektrum-Baender fuer die Analyser-Anzeige

// --- Voice-In (Mikrofon ueber den 0xd4-Opus-Duplex-Stream) --------------------
#define MIC_RATE         48000      // Mic-Opus: 48 kHz mono, 10 ms (480 Samples)
#define MIC_FRAME        480
#define MIC_TOC          0xD4       // konstanter Opus-TOC der Mic-Frames (= "Marker")
#define MIC_VOL          0x40       // mic_volume im State-Snapshot wenn Mic an
#define MIC_RING_FRAMES  48000      // 1 s Mono-Ringpuffer (decoded PCM)
#define MIC_PREBUF       4800       // ~100 ms Jitter-Polster bevor die Source spielt

// --- "Rumble as Subwoofer": Bass an die Haptik-Aktuatoren ---------------------
// Die Membran hat keinen Tiefbass; die zwei Voice-Coil-Haptik-Aktuatoren sind
// quasi Koerperschall-Subwoofer. Wir spiegeln einen tiefpassgefilterten Mono-
// Bass auf die Haptik-Route (0x12). Parameter aus ~/.DS5/config, live nachladbar.
struct subcfg {
    int   enabled;     // default 1 (an)
    float cutoff;      // Tiefpass-Cutoff Hz, default 200
    float gain;        // Haptik-Gain, default 2.6
    int   amp;         // int8-Amplituden-Cap (<=127), default 64
    int   web;         // Web/API-Dienst an? default 1
    int   web_port;    // default 8118
    int   microphone;  // Voice-In (Mic-Duplex 0xFF) an? default 0 (opt-in)
    int   jack;        // 0 = interner Speaker (0x13), 1 = Kopfhoerer-Klinke (0x16)
    int   leds;        // LED-Analyser (Lightbar-Farbe + Player-VU) an? default 0
};

// --- DualSense 0x31/0x36 Konstanten (aus DS5_Bridge bt.cpp/audio.cpp) ----------
static const uint8_t STATE_SNAPSHOT[STATE_SNAP_SIZE] = {
    0xfd, 0xe3, 0x00, 0x00, 0x7f, 0x64,
    0x00, 0x09, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00,  // mic_vol=0, power_save=mic_mute
    0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,0,
    0,0,0,0,0,0,0,0x0a, 0x04,0,0,0,0x01, 0x00, 0x00,0x00,0xff
};

// CRC-32 mit Sony-Seed 0xA2 (== zlib.crc32(0xA2 . data))
static uint32_t ds_crc(const uint8_t *data, size_t len) {
    uint8_t pre = 0xA2;
    uLong c = crc32(0L, &pre, 1);
    return (uint32_t) crc32(c, data, len);
}

struct data {
    struct pw_main_loop *loop;
    struct pw_stream *stream;
    OpusEncoder *enc;
    int hidraw_fd;
    uint8_t seq, counter;
    int fade_pos;
    int preroll_done;

    float acc[INPUT_BLOCK * SINK_CH];    // Akkumulator: [FL Membran, FR Haptik]
    int acc_frames;
    int write_errors;                    // -> Controller weg -> Loop beenden

    // Subwoofer (Haptik-Bass)
    struct subcfg cfg;
    float bq_b0, bq_b1, bq_b2, bq_a1, bq_a2;   // Biquad-LP-Koeffizienten
    float bq_x1, bq_x2, bq_y1, bq_y2;          // Biquad-Zustand
    float dc_x1, dc_y1;                        // One-Pole DC-Blocker-Zustand
    char  cfg_path[256];
    time_t cfg_mtime;
    int   cfg_ctr;

    // Web/API + Analyser (gemeinsamer Zustand, durch lock geschuetzt)
    pthread_mutex_t lock;
    int   cfg_apply;                  // Web aenderte cutoff -> Audio-Thread neu rechnen
    volatile int web_clients;         // >0 -> Analyser (FFT) ueberhaupt rechnen
    int   ana_bands[NBANDS];          // 0..100 pro Band (Spektrum-Snapshot)
    int   ana_mem, ana_hap;           // 0..100 Pegel Membran / Haptik
    int   ana_mic;                    // 0..100 Pegel Mikrofon
    int   led_ctr, leds_was;          // LED-Sende-Takt / Zustandswechsel-Erkennung

    // Voice-In: hidraw-Reader-Thread -> Opus-Decode -> Ringpuffer -> PW-Source
    struct pw_stream *src_stream;     // "DualSense BT Mic"
    char  hidraw_path[64];
    pthread_mutex_t mic_lock;
    float mic_ring[MIC_RING_FRAMES];
    int   mic_w, mic_r;               // Schreib-/Lese-Index (mono float)
    int   mic_ready;                  // 0 = Prebuffer (warte auf Polster), 1 = spielt
};

// RBJ-Tiefpass-Biquad (Q=0.707, Butterworth) — Koeffizienten setzen.
static void sub_set_cutoff(struct data *d, float fc) {
    if (fc < 20.f) fc = 20.f;
    if (fc > SRC_RATE / 2 - 100) fc = SRC_RATE / 2 - 100;
    double w0 = 2.0 * SUB_PI * fc / SRC_RATE;
    double cw = cos(w0), sw = sin(w0);
    double alpha = sw / (2.0 * 0.7071);
    double b0 = (1 - cw) / 2, b1 = 1 - cw, b2 = (1 - cw) / 2;
    double a0 = 1 + alpha, a1 = -2 * cw, a2 = 1 - alpha;
    d->bq_b0 = b0 / a0; d->bq_b1 = b1 / a0; d->bq_b2 = b2 / a0;
    d->bq_a1 = a1 / a0; d->bq_a2 = a2 / a0;
    d->cfg.cutoff = fc;
}

// ~/.DS5/config lesen (key = value, '#'=Kommentar). Stiller No-op ohne Datei.
static void load_config(struct data *d) {
    FILE *f = fopen(d->cfg_path, "r");
    if (!f) return;
    char line[256], key[64], val[64];
    float new_cut = d->cfg.cutoff;
    while (fgets(line, sizeof(line), f)) {
        if (line[0] == '#' || line[0] == '\n') continue;
        if (sscanf(line, " %63[a-zA-Z_] = %63s", key, val) == 2 ||
            sscanf(line, " %63[a-zA-Z_] %63s", key, val) == 2) {
            if (!strcmp(key, "subwoofer") || !strcmp(key, "enabled"))
                d->cfg.enabled = (!strcmp(val, "on") || !strcmp(val, "1") ||
                                  !strcmp(val, "true") || !strcmp(val, "yes"));
            else if (!strcmp(key, "cutoff_hz") || !strcmp(key, "cutoff"))
                new_cut = (float)atof(val);
            else if (!strcmp(key, "gain"))
                d->cfg.gain = (float)atof(val);
            else if (!strcmp(key, "amp")) {
                int a = atoi(val);
                d->cfg.amp = a < 1 ? 1 : (a > 127 ? 127 : a);
            }
            else if (!strcmp(key, "web"))
                d->cfg.web = (!strcmp(val, "on") || !strcmp(val, "1") ||
                              !strcmp(val, "true") || !strcmp(val, "yes"));
            else if (!strcmp(key, "web_port") || !strcmp(key, "port"))
                d->cfg.web_port = atoi(val);
            else if (!strcmp(key, "microphone") || !strcmp(key, "mic"))
                d->cfg.microphone = (!strcmp(val, "on") || !strcmp(val, "1") ||
                                     !strcmp(val, "true") || !strcmp(val, "yes"));
            else if (!strcmp(key, "output"))
                d->cfg.jack = (!strcmp(val, "jack") || !strcmp(val, "klinke") ||
                               !strcmp(val, "headphones") || !strcmp(val, "1"));
            else if (!strcmp(key, "leds"))
                d->cfg.leds = (!strcmp(val, "analyser") || !strcmp(val, "analyzer") ||
                               !strcmp(val, "on") || !strcmp(val, "1"));
        }
    }
    fclose(f);
    if (new_cut != d->cfg.cutoff) sub_set_cutoff(d, new_cut);
}

// Aktuelle Config zurueck in ~/.DS5/config schreiben (Web-Edit persistieren).
// Setzt cfg_mtime, damit der mtime-Reload dieselben Werte nicht erneut laedt.
static void save_config(struct data *d) {
    FILE *f = fopen(d->cfg_path, "w");
    if (!f) return;
    fprintf(f,
        "# DualSense BT Speaker — Konfiguration (live nachgeladen, ~1x/s).\n"
        "# Editierbar per Web-UI (http://localhost:%d) oder von Hand.\n"
        "\n"
        "# Audio-Ausgabe: 'speaker' = interne Membran, 'jack' = Kopfhoerer-Klinke.\n"
        "output    = %s\n"
        "\n"
        "# Rumble as Subwoofer: tiefpassgefilterter Bass an die Haptik-Aktuatoren.\n"
        "subwoofer = %s\n"
        "cutoff_hz = %.0f\n"
        "gain      = %.2f\n"
        "amp       = %d\n"
        "\n"
        "# Web/API-Dienst (Analyser + Tweak-UI), nur localhost.\n"
        "web       = %s\n"
        "web_port  = %d\n"
        "\n"
        "# Voice-In: DS5-Mikrofon als Aufnahmegeraet 'DualSense BT Mic'.\n"
        "# ACHTUNG: aktiviert den Mic-Duplex (Maske 0xFF) -> verseucht den\n"
        "# Gamepad-Input (Phantom-Stick). Nur einschalten, wenn du das Mic brauchst.\n"
        "microphone = %s\n"
        "\n"
        "# LED-Analyser: Lightbar-Farbe = Spektrum (Bass=Blau, Mitte=Rot, Hoehe=Weiss),\n"
        "# die 5 Player-LEDs als VU-Balken.\n"
        "leds      = %s\n",
        d->cfg.web_port, d->cfg.jack ? "jack" : "speaker",
        d->cfg.enabled ? "on" : "off", d->cfg.cutoff,
        d->cfg.gain, d->cfg.amp, d->cfg.web ? "on" : "off", d->cfg.web_port,
        d->cfg.microphone ? "on" : "off", d->cfg.leds ? "analyser" : "off");
    fclose(f);
    struct stat st;
    if (stat(d->cfg_path, &st) == 0) d->cfg_mtime = st.st_mtime;
}

// Default-Config beim ersten Start anlegen, damit der User die Params kennt.
static void write_default_config(struct data *d) {
    const char *home = getenv("HOME");
    if (!home) return;
    char dir[256];
    snprintf(dir, sizeof(dir), "%s/.DS5", home);
    mkdir(dir, 0755);
    save_config(d);
}

// Bass-Haptik aus dem 512-Frame-Block: mono -> LP -> DC-Block -> /8 -> 64 int8.
// Filtert ALLE 512 Samples (Reihenfolge!), behaelt je 8er-Gruppe das letzte.
static void build_haptic(struct data *d, const float *block512, uint8_t *hap) {
    if (!d->cfg.enabled) { memset(hap, 0, HAPTIC_N); return; }
    const int amp = d->cfg.amp;
    const float gain = d->cfg.gain;
    for (int i = 0; i < HAPTIC_N; i++) {
        float last = 0.f;
        for (int j = 0; j < HAPTIC_DECIM; j++) {
            int fr = i * HAPTIC_DECIM + j;
            float mono = block512[fr*SINK_CH + CH_HAPTIC];   // FR -> Haptik-Kanal
            float y = d->bq_b0*mono + d->bq_b1*d->bq_x1 + d->bq_b2*d->bq_x2
                      - d->bq_a1*d->bq_y1 - d->bq_a2*d->bq_y2;
            d->bq_x2 = d->bq_x1; d->bq_x1 = mono;
            d->bq_y2 = d->bq_y1; d->bq_y1 = y;
            last = y;
        }
        float dc = last - d->dc_x1 + 0.995f * d->dc_y1;   // DC-Blocker (~20 Hz HP)
        d->dc_x1 = last; d->dc_y1 = dc;
        int v = (int)lrintf(dc * gain * amp);
        if (v > amp) v = amp; else if (v < -amp) v = -amp;
        hap[i] = (uint8_t)(v & 0xFF);                     // int8 -> two's-complement byte
    }
}

// --- Analyser: 512-Punkt-FFT -> Baender, nur wenn ein Browser verbunden ist ---
static float g_hann[FFT_N];
static int   g_band_lo[NBANDS], g_band_hi[NBANDS];   // FFT-Bin-Bereich je Band
static float g_band_hz[NBANDS];                      // Band-Mittenfrequenz (Anzeige)
static int   g_ana_init = 0;

static void analyser_init(void) {
    for (int i = 0; i < FFT_N; i++)
        g_hann[i] = 0.5f * (1.f - cosf(2.f * (float)SUB_PI * i / (FFT_N - 1)));
    // Log-verteilte Baender von 40 Hz bis 16 kHz auf FFT-Bins abbilden.
    double f0 = 40.0, f1 = 16000.0;
    double binhz = (double)SRC_RATE / FFT_N;          // 93.75 Hz/Bin
    for (int b = 0; b < NBANDS; b++) {
        double lo = f0 * pow(f1 / f0, (double)b / NBANDS);
        double hi = f0 * pow(f1 / f0, (double)(b + 1) / NBANDS);
        int blo = (int)(lo / binhz), bhi = (int)(hi / binhz);
        if (blo < 1) blo = 1;
        if (bhi <= blo) bhi = blo + 1;
        if (bhi > FFT_N / 2) bhi = FFT_N / 2;
        g_band_lo[b] = blo; g_band_hi[b] = bhi;
        g_band_hz[b] = (float)sqrt(lo * hi);          // geometrische Mitte
    }
    g_ana_init = 1;
}

// Iterative Radix-2-FFT (in-place), N = FFT_N. re/im Laenge N.
static void fft512(float *re, float *im) {
    int n = FFT_N;
    for (int i = 1, j = 0; i < n; i++) {              // Bit-Reversal-Permutation
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { float t;
            t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t; }
    }
    for (int len = 2; len <= n; len <<= 1) {
        double ang = -2.0 * SUB_PI / len;
        float wr = (float)cos(ang), wi = (float)sin(ang);
        for (int i = 0; i < n; i += len) {
            float cr = 1.f, ci = 0.f;
            for (int k = 0; k < len / 2; k++) {
                int a = i + k, b = i + k + len / 2;
                float xr = re[b] * cr - im[b] * ci;
                float xi = re[b] * ci + im[b] * cr;
                re[b] = re[a] - xr; im[b] = im[a] - xi;
                re[a] += xr;        im[a] += xi;
                float ncr = cr * wr - ci * wi;
                ci = cr * wi + ci * wr; cr = ncr;
            }
        }
    }
}

static int db_to_level(float lin, float floor_db) {  // lin>0 -> 0..100
    float db = 20.f * log10f(lin + 1e-9f);
    int lv = (int)((db - floor_db) / (-floor_db) * 100.f);
    return lv < 0 ? 0 : (lv > 100 ? 100 : lv);
}

// Spektrum + Membran/Haptik-Pegel berechnen und unter lock als Snapshot ablegen.
static void analyser_compute(struct data *d, const float *block512,
                             const uint8_t *hap) {
    if (!g_ana_init) analyser_init();
    static float re[FFT_N], im[FFT_N];
    double msum = 0.0;
    for (int i = 0; i < FFT_N; i++) {
        float mono = block512[i*SINK_CH + CH_MEMBRANE];   // Spektrum vom Membran-Kanal
        msum += (double)mono * mono;
        re[i] = mono * g_hann[i];
        im[i] = 0.f;
    }
    fft512(re, im);
    int bands[NBANDS];
    for (int b = 0; b < NBANDS; b++) {
        double p = 0.0;
        for (int k = g_band_lo[b]; k < g_band_hi[b]; k++)
            p += (double)re[k]*re[k] + (double)im[k]*im[k];
        float mag = (float)(sqrt(p / (g_band_hi[b] - g_band_lo[b])) / (FFT_N / 2));
        bands[b] = db_to_level(mag, -80.f);
    }
    int mem = db_to_level((float)sqrt(msum / FFT_N), -70.f);
    // Haptik-Pegel aus den tatsaechlich gesendeten int8-Samples (RMS).
    double hsum = 0.0;
    for (int i = 0; i < HAPTIC_N; i++) {
        int s = (int8_t)hap[i];
        hsum += (double)s * s;
    }
    int hap_lv = db_to_level((float)sqrt(hsum / HAPTIC_N) / 127.f, -70.f);

    pthread_mutex_lock(&d->lock);
    for (int b = 0; b < NBANDS; b++) d->ana_bands[b] = bands[b];
    d->ana_mem = mem; d->ana_hap = hap_lv;
    pthread_mutex_unlock(&d->lock);
}

// --- LED-Analyser: Lightbar-Farbe + Player-VU via eigenem 0x31-Report ---------
// Bass -> Blau, Mitten -> Rot/Warm, Hoehen -> Weiss; Helligkeit = Pegel.
static void send_led(struct data *d) {
    int b[NBANDS];
    pthread_mutex_lock(&d->lock);
    for (int i = 0; i < NBANDS; i++) b[i] = d->ana_bands[i];
    pthread_mutex_unlock(&d->lock);
    int lo = 0, mi = 0, hi = 0;
    for (int i = 0;  i < 9;      i++) lo += b[i];
    for (int i = 9;  i < 19;     i++) mi += b[i];
    for (int i = 19; i < NBANDS; i++) hi += b[i];
    lo /= 9; mi /= 10; hi /= (NBANDS - 19);                         // ~40-250 / 250-2k / 2k-16k
    // Farb-Mix (0..100 -> 0..255): Bass=Blau, Mitte=Rot/Warm(R+G), Hoehe=Weiss
    int r  = (int)((mi * 1.00f + hi * 0.70f) * 2.55f);
    int g  = (int)((hi * 1.00f + mi * 0.25f) * 2.55f);
    int bl = (int)((lo * 1.00f + hi * 0.50f) * 2.55f);
    if (r  > 255) r  = 255;
    if (g  > 255) g  = 255;
    if (bl > 255) bl = 255;
    int lvl = lo > mi ? lo : mi;
    if (hi > lvl) lvl = hi;                                          // Gesamtpegel
    static const uint8_t bar[6] = {0x00,0x01,0x03,0x07,0x0F,0x1F};   // VU links->rechts
    int seg = lvl * 5 / 100;
    if (seg > 5) seg = 5;
    if (seg < 0) seg = 0;

    uint8_t p[78]; memset(p, 0, sizeof(p));
    p[0]=0x31; p[1]=(d->seq<<4)&0xF0; p[2]=0x10;
    p[4]=0x04|0x10;                          // valid_flag1: Lightbar + Player
    p[45]=0x02;                              // led_brightness (Player-LEDs)
    p[46]=bar[seg];                          // player_leds (VU-Balken)
    p[47]=(uint8_t)r; p[48]=(uint8_t)g; p[49]=(uint8_t)bl;
    uint32_t crc = ds_crc(p, 74);
    p[74]=crc&0xFF; p[75]=(crc>>8)&0xFF; p[76]=(crc>>16)&0xFF; p[77]=(crc>>24)&0xFF;
    if (write(d->hidraw_fd, p, 78) < 0) {}
}

// LED-Steuerung ans System zurueckgeben (valid_flag1 RELEASE_LEDS = 0x08).
static void send_led_release(struct data *d) {
    uint8_t p[78]; memset(p, 0, sizeof(p));
    p[0]=0x31; p[1]=(d->seq<<4)&0xF0; p[2]=0x10; p[4]=0x08;
    uint32_t crc = ds_crc(p, 74);
    p[74]=crc&0xFF; p[75]=(crc>>8)&0xFF; p[76]=(crc>>16)&0xFF; p[77]=(crc>>24)&0xFF;
    if (write(d->hidraw_fd, p, 78) < 0) {}
}

// --- 0x36-Paket bauen + senden ------------------------------------------------
static void send_block(struct data *d, const float *block512) {
    // Web-UI aenderte den Cutoff -> Biquad hier (Audio-Thread) neu rechnen.
    pthread_mutex_lock(&d->lock);
    if (d->cfg_apply) { sub_set_cutoff(d, d->cfg.cutoff); d->cfg_apply = 0; }
    pthread_mutex_unlock(&d->lock);

    // Config-Datei ~1x/s auf Aenderung pruefen -> live an/aus + Param-Tweak.
    if (++d->cfg_ctr >= CFG_CHECK_PKTS) {
        d->cfg_ctr = 0;
        struct stat st;
        if (stat(d->cfg_path, &st) == 0 && st.st_mtime != d->cfg_mtime) {
            pthread_mutex_lock(&d->lock);
            d->cfg_mtime = st.st_mtime;
            load_config(d);             // externer Datei-Edit (nicht via Web-UI)
            pthread_mutex_unlock(&d->lock);
        }
    }
    // Resample 512 -> 480 (linear), Fade-In, Float fuer opus_encode_float
    float out[OPUS_FRAME * CHANNELS];   // stereo fuer Opus (Controller erwartet 2ch)
    double step = (double)(INPUT_BLOCK - 1) / (OPUS_FRAME - 1);
    for (int i = 0; i < OPUS_FRAME; i++) {
        double src = i * step;
        int idx = (int)src;
        int nxt = idx < INPUT_BLOCK - 1 ? idx + 1 : idx;
        float frac = (float)(src - idx);
        float la = block512[idx*SINK_CH + CH_MEMBRANE];  // FL
        float lb = block512[nxt*SINK_CH + CH_MEMBRANE];
        float lv = la + (lb - la) * frac;
        float rv = lv;                                   // Speaker: Mono (R=L)
        if (d->cfg.jack) {                               // Klinke: echtes Stereo (R=FR)
            float ra = block512[idx*SINK_CH + CH_HAPTIC];
            float rb = block512[nxt*SINK_CH + CH_HAPTIC];
            rv = ra + (rb - ra) * frac;
        }
        if (d->fade_pos < FADE_SAMPLES) {
            int s = d->fade_pos + i;
            if (s < FADE_SAMPLES) { float g=(float)s/FADE_SAMPLES; lv*=g; rv*=g; }
        }
        out[i*CHANNELS] = lv; out[i*CHANNELS+1] = rv;
    }
    if (d->fade_pos < FADE_SAMPLES) d->fade_pos += OPUS_FRAME;

    uint8_t opus[SPEAKER_BYTES];
    int n = opus_encode_float(d->enc, out, OPUS_FRAME, opus, SPEAKER_BYTES);
    if (n < 0) return;

    uint8_t pkt[REPORT_SIZE];
    memset(pkt, 0, sizeof(pkt));
    pkt[0] = 0x36;
    pkt[1] = (d->seq & 0x0F) << 4;
    // 0x11 config: mask 0xFE (Bit0 clear = KEIN Mic-Capture/Duplex -> kein d4-
    // Stream im Input-Channel). DS5_Bridge nutzt 0xFF (Bit0=1) fuer Voice-Chat-
    // Duplex; wir wollen nur Speaker -> Bit0 raus. Verifiziert mit init_probe.py.
    // Maske 0xFE = nur Speaker (kein Mic-Duplex). 0xFF = Mic-Duplex AN (Voice-In),
    // bringt aber den d4-Stream zurueck -> Gamepad-Input wird verseucht.
    pkt[2] = 0x11 | 0x80;  pkt[3] = 7;  pkt[4] = d->cfg.microphone ? 0xFF : 0xFE;
    pkt[5]=pkt[6]=pkt[7]=pkt[8]=pkt[9] = 64;
    pkt[10] = d->counter;
    pkt[11] = 0x10 | 0x80; pkt[12] = STATE_SNAP_SIZE;
    memcpy(pkt + 13, STATE_SNAPSHOT, STATE_SNAP_SIZE);
    if (d->cfg.microphone) {                 // Mic entstummen (sonst Stille-Capture)
        pkt[13 + 6] = MIC_VOL;               // mic_volume
        pkt[13 + 9] = 0x00;                  // power_save: mic-mute-Bit raus
    }
    pkt[76] = 0x12 | 0x80; pkt[77] = HAPTIC_N;
    build_haptic(d, block512, pkt + 78);               // Haptik = Subwoofer-Bass
    pkt[142] = (d->cfg.jack ? 0x16 : 0x13) | 0x80;     // 0x13 Speaker / 0x16 Klinke
    pkt[143] = SPEAKER_BYTES;
    memcpy(pkt + 144, opus, n);                         // Rest ist 0 (CBR=200)
    uint32_t crc = ds_crc(pkt, REPORT_SIZE - 4);
    pkt[394]=crc&0xFF; pkt[395]=(crc>>8)&0xFF; pkt[396]=(crc>>16)&0xFF; pkt[397]=(crc>>24)&0xFF;

    if (write(d->hidraw_fd, pkt, REPORT_SIZE) < 0) {
        // Controller weg -> nach ~0.5s aufgeben, systemd startet uns neu.
        if (++d->write_errors > 50) pw_main_loop_quit(d->loop);
    } else {
        d->write_errors = 0;
    }
    d->seq = (d->seq + 1) & 0x0F;
    d->counter = (d->counter + 1) & 0xFF;

    // Analyser rechnen, wenn ein Browser verbunden ist ODER der LED-Analyser laeuft.
    if (d->web_clients > 0 || d->cfg.leds) analyser_compute(d, block512, pkt + 78);

    // LED-Analyser: Zustandswechsel -> ggf. LEDs ans System zurueckgeben;
    // sonst ~19x/s einen 0x31-LED-Report mit der aktuellen Spektral-Farbe.
    if (d->cfg.leds != d->leds_was) {
        if (!d->cfg.leds) send_led_release(d);
        d->leds_was = d->cfg.leds;
    }
    if (d->cfg.leds && ++d->led_ctr >= 5) { d->led_ctr = 0; send_led(d); }
}

// --- 0x31 Speaker-Enable ------------------------------------------------------
static void send_setup(int fd) {
    uint8_t r[78]; memset(r, 0, sizeof(r));
    r[0]=0x31; r[1]=0x10;
    r[3]=0xA0;  // valid_flag0: AUDIO_CONTROL_ENABLE|SPEAKER_VOLUME_ENABLE
    r[4]=0x80;  // valid_flag1: AUDIO_CONTROL2_ENABLE
    r[8]=0x64;  // speaker_volume
    r[10]=0x30; // audio_control = PATH_SPEAKER
    r[40]=0x02; // audio_control2 = preamp
    uint32_t crc = ds_crc(r, 74);
    r[74]=crc&0xFF; r[75]=(crc>>8)&0xFF; r[76]=(crc>>16)&0xFF; r[77]=(crc>>24)&0xFF;
    if (write(fd, r, 78) < 0) {}
}

// --- PipeWire process-callback ------------------------------------------------
static void on_process(void *userdata) {
    struct data *d = userdata;
    struct pw_buffer *b;
    if ((b = pw_stream_dequeue_buffer(d->stream)) == NULL) return;

    struct spa_buffer *buf = b->buffer;
    float *samples = buf->datas[0].data;
    if (samples) {
        uint32_t n_bytes = buf->datas[0].chunk->size;
        uint32_t n_frames = n_bytes / (sizeof(float) * SINK_CH);
        for (uint32_t f = 0; f < n_frames; f++) {
            d->acc[d->acc_frames*SINK_CH + CH_MEMBRANE] = samples[f*SINK_CH + CH_MEMBRANE];
            d->acc[d->acc_frames*SINK_CH + CH_HAPTIC]   = samples[f*SINK_CH + CH_HAPTIC];
            if (++d->acc_frames >= INPUT_BLOCK) {
                send_block(d, d->acc);
                d->acc_frames = 0;
            }
        }
    }
    pw_stream_queue_buffer(d->stream, b);
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .process = on_process,
};

// --- hidraw finden ------------------------------------------------------------
static int find_hidraw(char *out, size_t outlen) {
    glob_t g;
    if (glob("/sys/class/hidraw/hidraw*/device/uevent", 0, NULL, &g) != 0) return -1;
    int found = -1;
    for (size_t i = 0; i < g.gl_pathc; i++) {
        FILE *f = fopen(g.gl_pathv[i], "r");
        if (!f) continue;
        char line[256]; int hit = 0;
        while (fgets(line, sizeof(line), f))
            if (strstr(line, "HID_ID=0005:0000054C:00000CE6")) { hit = 1; break; }
        fclose(f);
        if (hit) {
            char node[32];
            if (sscanf(g.gl_pathv[i], "/sys/class/hidraw/%31[^/]", node) == 1) {
                snprintf(out, outlen, "/dev/%s", node);
                found = 0;
            }
            break;
        }
    }
    globfree(&g);
    return found;
}

// ============================ Web/API + Analyser ==============================
// Eingebetteter HTTP+WebSocket-Server (nur 127.0.0.1). Kein externer Dep.
// Browser-UI: Analyser (Membran/Haptik-Split) + Config-Tweak per WebSocket.

static void sha1(const uint8_t *msg, size_t len, uint8_t out[20]) {
    uint32_t h0=0x67452301,h1=0xEFCDAB89,h2=0x98BADCFE,h3=0x10325476,h4=0xC3D2E1F0;
    size_t ml = len * 8, total = ((len + 8) / 64 + 1) * 64;
    uint8_t *m = calloc(total, 1);
    if (!m) return;
    memcpy(m, msg, len); m[len] = 0x80;
    for (int i = 0; i < 8; i++) m[total-1-i] = (uint8_t)((ml >> (8*i)) & 0xFF);
    for (size_t off = 0; off < total; off += 64) {
        uint32_t w[80];
        for (int i = 0; i < 16; i++)
            w[i] = (m[off+4*i]<<24)|(m[off+4*i+1]<<16)|(m[off+4*i+2]<<8)|m[off+4*i+3];
        for (int i = 16; i < 80; i++) { uint32_t v=w[i-3]^w[i-8]^w[i-14]^w[i-16];
            w[i]=(v<<1)|(v>>31); }
        uint32_t a=h0,b=h1,c=h2,d=h3,e=h4;
        for (int i = 0; i < 80; i++) {
            uint32_t f,k;
            if (i<20){f=(b&c)|((~b)&d);k=0x5A827999;}
            else if (i<40){f=b^c^d;k=0x6ED9EBA1;}
            else if (i<60){f=(b&c)|(b&d)|(c&d);k=0x8F1BBCDC;}
            else {f=b^c^d;k=0xCA62C1D6;}
            uint32_t t=((a<<5)|(a>>27))+f+e+k+w[i];
            e=d;d=c;c=(b<<30)|(b>>2);b=a;a=t;
        }
        h0+=a;h1+=b;h2+=c;h3+=d;h4+=e;
    }
    free(m);
    uint32_t hs[5]={h0,h1,h2,h3,h4};
    for (int i=0;i<5;i++){ out[4*i]=(hs[i]>>24)&0xFF; out[4*i+1]=(hs[i]>>16)&0xFF;
        out[4*i+2]=(hs[i]>>8)&0xFF; out[4*i+3]=hs[i]&0xFF; }
}

static void b64(const uint8_t *in, int n, char *out) {
    static const char *t="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    int i, o=0;
    for (i=0;i+2<n;i+=3){ out[o++]=t[in[i]>>2]; out[o++]=t[((in[i]&3)<<4)|(in[i+1]>>4)];
        out[o++]=t[((in[i+1]&15)<<2)|(in[i+2]>>6)]; out[o++]=t[in[i+2]&63]; }
    int rem=n-i;
    if (rem==1){ out[o++]=t[in[i]>>2]; out[o++]=t[(in[i]&3)<<4]; out[o++]='='; out[o++]='='; }
    else if (rem==2){ out[o++]=t[in[i]>>2]; out[o++]=t[((in[i]&3)<<4)|(in[i+1]>>4)];
        out[o++]=t[(in[i+1]&15)<<2]; out[o++]='='; }
    out[o]=0;
}

static const char PAGE[] =
"<!doctype html><html lang=de><head><meta charset=utf-8>"
"<meta name=viewport content='width=device-width,initial-scale=1'>"
"<title>DualSense BT Speaker</title><style>"
"body{font-family:system-ui,sans-serif;background:#d6d6d6;color:#222;margin:0;padding:18px}"
".card{background:#f0f0f0;border:1px solid #bbb;border-radius:10px;padding:16px;"
"max-width:760px;margin:0 auto 14px;box-shadow:0 1px 3px rgba(0,0,0,.12)}"
"h1{font-size:19px;margin:0 0 2px}.sub{color:#666;font-size:13px;margin:0 0 12px}"
"#stat{font-size:12px;color:#777}"
"canvas{width:100%;height:200px;background:#fafafa;border:1px solid #ccc;border-radius:6px;display:block}"
".legend{font-size:12px;color:#555;margin-top:6px}"
".sw{display:inline-block;width:11px;height:11px;border-radius:2px;vertical-align:middle;margin:0 4px 0 12px}"
".meter{height:16px;background:#e2e2e2;border:1px solid #c4c4c4;border-radius:4px;overflow:hidden;margin:3px 0 10px}"
".meter>div{height:100%;width:0;transition:width .05s}"
".row{display:flex;align-items:center;gap:10px;margin:10px 0}"
".row label{width:92px;font-size:14px}.row output{width:64px;text-align:right;font-variant-numeric:tabular-nums}"
"input[type=range]{flex:1;accent-color:#3a7ca5}"
"input[type=checkbox]{width:18px;height:18px;accent-color:#3a7ca5}"
"</style></head><body>"
"<div class=card><h1>DualSense BT Speaker</h1>"
"<p class=sub>Rumble as Subwoofer &middot; Analyser &amp; Tweak &middot; <span id=stat>verbinde&hellip;</span></p>"
"<canvas id=cv width=720 height=200></canvas>"
"<div class=legend><span class=sw style=background:#d98a2b></span>Haptik (Bass &lt; Cutoff)"
"<span class=sw style=background:#3a7ca5></span>Membran (&ge; Cutoff)"
"<span style=color:#c0392b;margin-left:12px>&#9474;</span> Cutoff</div></div>"
"<div class=card>"
"<div style='font-size:13px;color:#555'>Pegel Membran</div><div class=meter><div id=mm style=background:#3a7ca5></div></div>"
"<div style='font-size:13px;color:#555'>Pegel Haptik</div><div class=meter><div id=mh style=background:#d98a2b></div></div>"
"<div style='font-size:13px;color:#555'>Pegel Mikrofon</div><div class=meter><div id=mi style=background:#5a9e5a></div></div>"
"<div class=row><label>Ausgabe</label><input type=checkbox id=jack><output id=jack_v></output></div>"
"<div style='font-size:11px;color:#999;margin-top:-4px'>Kopfh&ouml;rer-Klinke (an) statt interner Membran (aus). Klinke = echtes Stereo.</div>"
"<div class=row><label>Subwoofer</label><input type=checkbox id=on><output id=on_v></output></div>"
"<div class=row><label>Cutoff</label><input type=range id=cut min=40 max=400 step=5><output id=cut_v></output></div>"
"<div class=row><label>Gain</label><input type=range id=gain min=0 max=8 step=0.1><output id=gain_v></output></div>"
"<div class=row><label>Amp-Cap</label><input type=range id=amp min=1 max=127 step=1><output id=amp_v></output></div>"
"<div class=row><label>Mikrofon</label><input type=checkbox id=mic><output id=mic_v></output></div>"
"<div style='font-size:11px;color:#999;margin-top:-4px'>Voice-In als Aufnahmeger&auml;t. Achtung: verseucht den Gamepad-Input.</div>"
"<div class=row><label>LED-Analyser</label><input type=checkbox id=leds><output id=leds_v></output></div>"
"<div style='font-size:11px;color:#999;margin-top:-4px'>Lightbar-Farbe = Spektrum (Bass=Blau, Mitte=Rot, H&ouml;he=Wei&szlig;), 5 LEDs = VU.</div>"
"</div><script>"
"let ws,bf=null,init=false;"
"const cv=document.getElementById('cv'),cx=cv.getContext('2d');"
"function st(s){document.getElementById('stat').textContent=s;}"
"function send(k,v){if(ws&&ws.readyState==1)ws.send(JSON.stringify({k:k,v:v}));}"
"function bind(id,key,fmt){let el=document.getElementById(id),o=document.getElementById(id+'_v');"
"el.addEventListener('input',()=>{o.textContent=fmt(el.value);send(key,parseFloat(el.value));});}"
"document.getElementById('on').addEventListener('change',e=>{"
"document.getElementById('on_v').textContent=e.target.checked?'an':'aus';send('on',e.target.checked?1:0);});"
"document.getElementById('mic').addEventListener('change',e=>{"
"document.getElementById('mic_v').textContent=e.target.checked?'an':'aus';send('mic',e.target.checked?1:0);});"
"document.getElementById('jack').addEventListener('change',e=>{"
"document.getElementById('jack_v').textContent=e.target.checked?'Klinke':'Membran';send('jack',e.target.checked?1:0);});"
"document.getElementById('leds').addEventListener('change',e=>{"
"document.getElementById('leds_v').textContent=e.target.checked?'an':'aus';send('leds',e.target.checked?1:0);});"
"bind('cut','cut',v=>v+' Hz');bind('gain','gain',v=>(+v).toFixed(1));bind('amp','amp',v=>v);"
"function draw(b,cut){let W=cv.width,H=cv.height;cx.clearRect(0,0,W,H);if(!bf)return;"
"let n=b.length,bw=W/n;for(let i=0;i<n;i++){let h=b[i]/100*H;"
"cx.fillStyle=(bf[i]<cut)?'#d98a2b':'#3a7ca5';cx.fillRect(i*bw+1,H-h,bw-2,h);}"
"let ci=0;for(let i=0;i<n;i++){if(bf[i]>=cut){ci=i;break;}}let x=ci*bw;"
"cx.strokeStyle='#c0392b';cx.lineWidth=2;cx.beginPath();cx.moveTo(x,0);cx.lineTo(x,H);cx.stroke();}"
"function update(m){document.getElementById('mm').style.width=m.mem+'%';"
"document.getElementById('mh').style.width=m.hap+'%';"
"document.getElementById('mi').style.width=(m.miclv||0)+'%';draw(m.b,m.cut);"
"if(!init){init=true;let on=document.getElementById('on');on.checked=!!m.on;"
"document.getElementById('on_v').textContent=m.on?'an':'aus';"
"let mc=document.getElementById('mic');mc.checked=!!m.mic;"
"document.getElementById('mic_v').textContent=m.mic?'an':'aus';"
"let jk=document.getElementById('jack');jk.checked=!!m.jack;"
"document.getElementById('jack_v').textContent=m.jack?'Klinke':'Membran';"
"let ld=document.getElementById('leds');ld.checked=!!m.leds;"
"document.getElementById('leds_v').textContent=m.leds?'an':'aus';"
"let c=document.getElementById('cut');c.value=m.cut;document.getElementById('cut_v').textContent=m.cut+' Hz';"
"let g=document.getElementById('gain');g.value=m.gain;document.getElementById('gain_v').textContent=(+m.gain).toFixed(1);"
"let a=document.getElementById('amp');a.value=m.amp;document.getElementById('amp_v').textContent=m.amp;}}"
"function conn(){ws=new WebSocket('ws://'+location.host+'/ws');"
"ws.onopen=()=>st('verbunden');ws.onclose=()=>{st('getrennt \\u2014 reconnect\\u2026');init=false;setTimeout(conn,1000);};"
"ws.onmessage=e=>{let m=JSON.parse(e.data);if(m.bf){bf=m.bf;return;}update(m);};}"
"function fit(){cv.width=cv.clientWidth;cv.height=cv.clientHeight;}"
"addEventListener('resize',fit);fit();conn();</script></body></html>";

static char g_bf_json[512];   // {"bf":[..]} einmal pro Client gesendet

static int ws_send(int fd, const char *msg) {
    size_t len = strlen(msg);
    uint8_t hdr[4]; int hl;
    if (len < 126) { hdr[0]=0x81; hdr[1]=(uint8_t)len; hl=2; }
    else { hdr[0]=0x81; hdr[1]=126; hdr[2]=(len>>8)&0xFF; hdr[3]=len&0xFF; hl=4; }
    if (send(fd, hdr, hl, MSG_NOSIGNAL) < 0) return -1;
    if (send(fd, msg, len, MSG_NOSIGNAL) < 0) return -1;
    return 0;
}

// Eingehende WS-Config-Nachricht: {"k":"cut","v":150} (whitespace-tolerant).
static void web_apply(struct data *d, const char *payload) {
    char k[16] = {0}; double v;
    const char *kp = strstr(payload, "\"k\"");
    const char *vp = strstr(payload, "\"v\"");
    if (!kp || !vp) return;
    kp = strchr(kp + 3, ':'); if (!kp) return;
    while (*kp == ':' || *kp == ' ' || *kp == '"') kp++;
    int i = 0; while (*kp && *kp != '"' && i < 15) k[i++] = *kp++;
    k[i] = 0;
    vp = strchr(vp + 3, ':'); if (!vp) return;
    v = atof(vp + 1);
    pthread_mutex_lock(&d->lock);
    if (!strcmp(k, "cut")) {
        d->cfg.cutoff = (float)(v < 20 ? 20 : (v > 2000 ? 2000 : v));
        d->cfg_apply = 1;
    } else if (!strcmp(k, "gain")) {
        d->cfg.gain = (float)(v < 0 ? 0 : (v > 8 ? 8 : v));
    } else if (!strcmp(k, "amp")) {
        int a = (int)v; d->cfg.amp = a < 1 ? 1 : (a > 127 ? 127 : a);
    } else if (!strcmp(k, "on")) {
        d->cfg.enabled = (v != 0);
    } else if (!strcmp(k, "mic")) {
        d->cfg.microphone = (v != 0);
    } else if (!strcmp(k, "jack")) {
        d->cfg.jack = (v != 0);
    } else if (!strcmp(k, "leds")) {
        d->cfg.leds = (v != 0);
    }
    save_config(d);                 // persistieren (+ cfg_mtime, kein Reload-Echo)
    pthread_mutex_unlock(&d->lock);
}

static int http_read(int fd, char *buf, int cap) {
    int n = 0;
    while (n < cap - 1) {
        int r = recv(fd, buf + n, cap - 1 - n, 0);
        if (r <= 0) break;
        n += r;
        buf[n] = 0;
        if (strstr(buf, "\r\n\r\n")) break;
    }
    buf[n] = 0;
    return n;
}

// Neue Verbindung annehmen: WebSocket-Handshake oder die Seite ausliefern.
// Rueckgabe: fd (>=0) wenn es ein WS-Client wurde, sonst -1 (Seite + close).
static int web_accept(int lfd) {
    struct sockaddr_in ca; socklen_t cl = sizeof(ca);
    int fd = accept(lfd, (struct sockaddr*)&ca, &cl);
    if (fd < 0) return -1;
    struct timeval tv = {3, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    char req[2048];
    if (http_read(fd, req, sizeof(req)) <= 0) { close(fd); return -1; }
    char *key = strstr(req, "Sec-WebSocket-Key:");
    if (!key) {                                   // normale HTTP-Anfrage -> Seite
        char hdr[256];
        int len = (int)strlen(PAGE);
        int hl = snprintf(hdr, sizeof(hdr),
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n", len);
        send(fd, hdr, hl, MSG_NOSIGNAL);
        send(fd, PAGE, len, MSG_NOSIGNAL);
        close(fd);
        return -1;
    }
    key += 18;
    while (*key == ' ') key++;
    char k[128]; int i = 0;
    while (*key && *key != '\r' && *key != '\n' && i < 100) k[i++] = *key++;
    k[i] = 0;
    char cat[200]; snprintf(cat, sizeof(cat),
        "%s258EAFA5-E914-47DA-95CA-C5AB0DC85B11", k);
    uint8_t dig[20]; sha1((uint8_t*)cat, strlen(cat), dig);
    char acc[40]; b64(dig, 20, acc);
    char resp[256];
    int rl = snprintf(resp, sizeof(resp),
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n", acc);
    send(fd, resp, rl, MSG_NOSIGNAL);
    tv.tv_sec = 0; setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    fcntl(fd, F_SETFL, O_NONBLOCK);
    ws_send(fd, g_bf_json);                       // Band-Frequenzen einmalig
    return fd;
}

// Eingehende WS-Frames verarbeiten. -1 = Verbindung schliessen. Mehrere Frames
// koennen TCP-koalesziert in einem recv() ankommen -> alle im Puffer abarbeiten.
static int web_recv(struct data *d, int fd) {
    uint8_t b[4096];
    int n = recv(fd, b, sizeof(b), 0);
    if (n <= 0) return (n == 0) ? -1 : (errno == EAGAIN ? 0 : -1);
    int pos = 0;
    while (pos + 2 <= n) {
        int opcode = b[pos] & 0x0F, masked = b[pos+1] & 0x80;
        int len = b[pos+1] & 0x7F, idx = pos + 2;
        if (len == 126) { if (pos + 4 > n) break; len = (b[pos+2]<<8)|b[pos+3]; idx = pos + 4; }
        if (opcode == 0x8) return -1;             // close
        if (!masked || len > 1024 || idx + 4 + len > n) break;
        uint8_t *mask = b + idx, *pl = b + idx + 4;
        for (int i = 0; i < len; i++) pl[i] ^= mask[i & 3];
        uint8_t saved = pl[len]; pl[len] = 0;
        if (opcode == 0x1) web_apply(d, (char*)pl);  // Text -> Config
        pl[len] = saved;
        pos = idx + 4 + len;
    }
    return 0;
}

static void *web_thread(void *arg) {
    struct data *d = arg;
    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    if (lfd < 0) return NULL;
    int one = 1; setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in a; memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);   // nur 127.0.0.1
    a.sin_port = htons((uint16_t)d->cfg.web_port);
    if (bind(lfd, (struct sockaddr*)&a, sizeof(a)) < 0 || listen(lfd, 8) < 0) {
        fprintf(stderr, "Web-UI: Port %d nicht bindbar (%s) — UI aus.\n",
                d->cfg.web_port, strerror(errno));
        close(lfd); return NULL;
    }
    fprintf(stderr, "Web-UI: http://localhost:%d\n", d->cfg.web_port);

    int cfd[WEB_MAX_CLIENTS]; for (int i = 0; i < WEB_MAX_CLIENTS; i++) cfd[i] = -1;
    struct timespec last; clock_gettime(CLOCK_MONOTONIC, &last);
    for (;;) {
        struct pollfd pfd[WEB_MAX_CLIENTS + 1];
        pfd[0].fd = lfd; pfd[0].events = POLLIN;
        int nf = 1;
        for (int i = 0; i < WEB_MAX_CLIENTS; i++)
            if (cfd[i] >= 0) { pfd[nf].fd = cfd[i]; pfd[nf].events = POLLIN; nf++; }
        poll(pfd, nf, 50);

        if (pfd[0].revents & POLLIN) {
            int slot = -1;
            for (int i = 0; i < WEB_MAX_CLIENTS; i++) if (cfd[i] < 0) { slot = i; break; }
            int nfd = web_accept(lfd);
            if (nfd >= 0) {
                if (slot < 0) { close(nfd); }      // voll -> ablehnen
                else { cfd[slot] = nfd; d->web_clients++; }
            }
        }
        for (int p = 1; p < nf; p++) {
            if (!(pfd[p].revents & (POLLIN|POLLHUP|POLLERR))) continue;
            int fd = pfd[p].fd;
            if (web_recv(d, fd) < 0) {
                for (int i = 0; i < WEB_MAX_CLIENTS; i++) if (cfd[i] == fd) cfd[i] = -1;
                close(fd); d->web_clients--;
            }
        }
        // ~20x/s Analyser+Config an alle Clients senden
        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        double ms = (now.tv_sec-last.tv_sec)*1000.0 + (now.tv_nsec-last.tv_nsec)/1e6;
        if (ms >= 45.0) {
            last = now;
            char msg[768]; int o;
            pthread_mutex_lock(&d->lock);
            o = snprintf(msg, sizeof(msg),
                "{\"on\":%d,\"cut\":%.0f,\"gain\":%.2f,\"amp\":%d,\"mic\":%d,\"jack\":%d,\"leds\":%d,"
                "\"mem\":%d,\"hap\":%d,\"miclv\":%d,\"b\":[",
                d->cfg.enabled, d->cfg.cutoff, d->cfg.gain, d->cfg.amp,
                d->cfg.microphone, d->cfg.jack, d->cfg.leds,
                d->ana_mem, d->ana_hap, d->ana_mic);
            for (int bnd = 0; bnd < NBANDS; bnd++)
                o += snprintf(msg+o, sizeof(msg)-o, "%s%d", bnd?",":"", d->ana_bands[bnd]);
            pthread_mutex_unlock(&d->lock);
            o += snprintf(msg+o, sizeof(msg)-o, "]}");
            for (int i = 0; i < WEB_MAX_CLIENTS; i++)
                if (cfd[i] >= 0 && ws_send(cfd[i], msg) < 0) {
                    close(cfd[i]); cfd[i] = -1; d->web_clients--;
                }
        }
    }
    return NULL;
}

static void web_ui_start(struct data *d) {
    analyser_init();                              // Band-Frequenzen bereitstellen
    int o = snprintf(g_bf_json, sizeof(g_bf_json), "{\"bf\":[");
    for (int b = 0; b < NBANDS; b++)
        o += snprintf(g_bf_json+o, sizeof(g_bf_json)-o, "%s%d", b?",":"",
                      (int)(g_band_hz[b] + 0.5f));
    snprintf(g_bf_json+o, sizeof(g_bf_json)-o, "]}");
    pthread_t th;
    pthread_create(&th, NULL, web_thread, d);
    pthread_detach(th);
}

// ============================ Voice-In (Mikrofon) =============================
// Liest hidraw, filtert die d4-Opus-Mic-Frames, dekodiert -> Mono-Ringpuffer.
// Die PipeWire-Source unten zieht daraus. Decodiert nur wenn microphone=on.
static void *mic_thread(void *arg) {
    struct data *d = arg;
    int derr;
    OpusDecoder *dec = opus_decoder_create(MIC_RATE, 1, &derr);
    if (derr != OPUS_OK) return NULL;
    int16_t pcm[MIC_FRAME];
    uint8_t buf[256];
    for (;;) {
        int fd = open(d->hidraw_path, O_RDONLY);
        if (fd < 0) { sleep(1); continue; }
        for (;;) {
            int n = read(fd, buf, sizeof(buf));
            if (n <= 0) break;                       // Controller weg -> reopen
            if (buf[0] != 0x31 || n < 16 || buf[3] != MIC_TOC) continue;
            if (!d->cfg.microphone) continue;        // Mic aus -> ignorieren
            int ns = opus_decode(dec, buf + 3, n - 3 - 4, pcm, MIC_FRAME, 0);
            if (ns <= 0) continue;                   // n-3-4: 4 Byte CRC weglassen
            double sum = 0.0;
            pthread_mutex_lock(&d->mic_lock);
            for (int i = 0; i < ns; i++) {
                float f = pcm[i] / 32768.f;
                sum += (double)f * f;
                d->mic_ring[d->mic_w] = f;
                d->mic_w = (d->mic_w + 1) % MIC_RING_FRAMES;
                if (d->mic_w == d->mic_r)            // Overrun -> aeltestes verwerfen
                    d->mic_r = (d->mic_r + 1) % MIC_RING_FRAMES;
            }
            pthread_mutex_unlock(&d->mic_lock);
            if (d->web_clients > 0) {
                int lv = db_to_level((float)sqrt(sum / ns), -70.f);
                pthread_mutex_lock(&d->lock); d->ana_mic = lv; pthread_mutex_unlock(&d->lock);
            }
        }
        close(fd);
    }
    return NULL;
}

// PipeWire-Source-Callback: Mono-PCM aus dem Ring in den Graph schieben.
static void on_process_src(void *userdata) {
    struct data *d = userdata;
    struct pw_buffer *b = pw_stream_dequeue_buffer(d->src_stream);
    if (!b) return;
    struct spa_buffer *buf = b->buffer;
    float *dst = buf->datas[0].data;
    if (dst) {
        uint32_t maxf = buf->datas[0].maxsize / sizeof(float);
        // Quantum: was der Graph anfordert; Fallback 512 (Source node.latency).
        uint32_t req = (b->requested && (uint32_t)b->requested <= maxf)
                       ? (uint32_t)b->requested : (maxf < 512 ? maxf : 512);
        pthread_mutex_lock(&d->mic_lock);
        uint32_t avail = (uint32_t)((d->mic_w - d->mic_r + MIC_RING_FRAMES) % MIC_RING_FRAMES);
        if (!d->mic_ready && avail >= MIC_PREBUF) d->mic_ready = 1;  // Polster voll -> los
        for (uint32_t i = 0; i < req; i++) {            // IMMER volle req Frames liefern
            if (d->mic_ready && d->mic_r != d->mic_w) {
                dst[i] = d->mic_ring[d->mic_r];
                d->mic_r = (d->mic_r + 1) % MIC_RING_FRAMES;
            } else {
                dst[i] = 0.f;                           // Prebuffer / echter Underrun
                d->mic_ready = 0;                       // -> Polster neu aufbauen
            }
        }
        pthread_mutex_unlock(&d->mic_lock);
        buf->datas[0].chunk->offset = 0;
        buf->datas[0].chunk->stride = sizeof(float);
        buf->datas[0].chunk->size = req * sizeof(float);
    }
    pw_stream_queue_buffer(d->src_stream, b);
}

static const struct pw_stream_events src_stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .process = on_process_src,
};

int main(int argc, char **argv) {
    pw_init(&argc, &argv);
    struct data d; memset(&d, 0, sizeof(d));

    // --wait: warte auf den Controller statt sofort zu beenden (fuer den
    // systemd-Service, der dauerhaft laeuft und auf BT-Connect reagiert).
    int opt_wait = 0;
    for (int i = 1; i < argc; i++)
        if (strcmp(argv[i], "--wait") == 0) opt_wait = 1;

    char hidraw[64];
    while (find_hidraw(hidraw, sizeof(hidraw)) < 0) {
        if (!opt_wait) {
            fprintf(stderr, "DualSense BT hidraw nicht gefunden.\n"); return 1;
        }
        sleep(2);   // warten bis der Controller per BT verbindet
    }
    d.hidraw_fd = open(hidraw, O_WRONLY);
    if (d.hidraw_fd < 0) { perror("open hidraw"); return 1; }
    snprintf(d.hidraw_path, sizeof(d.hidraw_path), "%s", hidraw);
    pthread_mutex_init(&d.mic_lock, NULL);
    fprintf(stderr, "DualSense: %s\n", hidraw);

    int err;
    d.enc = opus_encoder_create(SRC_RATE, CHANNELS, OPUS_APPLICATION_AUDIO, &err);
    if (err != OPUS_OK) { fprintf(stderr, "opus init: %s\n", opus_strerror(err)); return 1; }
    opus_encoder_ctl(d.enc, OPUS_SET_BITRATE(OPUS_BITRATE));
    opus_encoder_ctl(d.enc, OPUS_SET_VBR(0));            // CBR -> feste 200 byte
    opus_encoder_ctl(d.enc, OPUS_SET_COMPLEXITY(0));

    // Subwoofer-Defaults + Config (~/.DS5/config beim ersten Start anlegen).
    pthread_mutex_init(&d.lock, NULL);
    d.cfg.enabled = 1; d.cfg.cutoff = 200.f; d.cfg.gain = 2.6f; d.cfg.amp = 64;
    d.cfg.web = 1; d.cfg.web_port = WEB_PORT_DEFAULT;
    const char *home = getenv("HOME");
    if (home) snprintf(d.cfg_path, sizeof(d.cfg_path), "%s/.DS5/config", home);
    struct stat st;
    if (home && stat(d.cfg_path, &st) != 0) write_default_config(&d);
    sub_set_cutoff(&d, d.cfg.cutoff);                   // Biquad initialisieren
    load_config(&d);                                    // ggf. User-Werte uebernehmen
    if (stat(d.cfg_path, &st) == 0) d.cfg_mtime = st.st_mtime;
    fprintf(stderr, "Subwoofer: %s  cutoff=%.0f Hz gain=%.2f amp=%d  (~/.DS5/config)\n",
            d.cfg.enabled ? "an" : "aus", d.cfg.cutoff, d.cfg.gain, d.cfg.amp);
    fprintf(stderr, "Mikrofon (Voice-In): %s%s\n", d.cfg.microphone ? "an" : "aus",
            d.cfg.microphone ? "  — Achtung: Gamepad-Input wird verseucht" : "");

    if (d.cfg.web) web_ui_start(&d);

    send_setup(d.hidraw_fd);
    // Preroll: Stille-Frames laufen einfach mit (acc startet leer -> Stille)
    (void)PREROLL_PKTS;

    d.loop = pw_main_loop_new(NULL);
    d.stream = pw_stream_new_simple(
        pw_main_loop_get_loop(d.loop),
        "DualSense BT Speaker",
        pw_properties_new(
            PW_KEY_MEDIA_TYPE, "Audio",
            PW_KEY_MEDIA_CATEGORY, "Playback",
            PW_KEY_MEDIA_CLASS, "Audio/Sink",
            PW_KEY_NODE_NAME, "ds5_membrane",
            PW_KEY_NODE_DESCRIPTION, "DualSense BT Speaker",
            PW_KEY_NODE_LATENCY, "512/48000",   // quantum -> 10.667ms Process-Takt
            NULL),
        &stream_events, &d);

    uint8_t buffer[1024];
    struct spa_pod_builder bld = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    struct spa_audio_info_raw info = {
        .format = SPA_AUDIO_FORMAT_F32,
        .rate = SRC_RATE,
        .channels = SINK_CH,                 // FL = Membran, FR = Haptik
        .position = { SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR },
    };
    const struct spa_pod *params[1];
    params[0] = spa_format_audio_raw_build(&bld, SPA_PARAM_EnumFormat, &info);

    pw_stream_connect(d.stream,
        PW_DIRECTION_INPUT,                 // Sink = empfaengt Audio
        PW_ID_ANY,
        PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS |
        PW_STREAM_FLAG_RT_PROCESS,
        params, 1);

    // --- PipeWire-Source "DualSense BT Mic" (Voice-In) -----------------------
    d.src_stream = pw_stream_new_simple(
        pw_main_loop_get_loop(d.loop),
        "DualSense BT Mic",
        pw_properties_new(
            PW_KEY_MEDIA_TYPE, "Audio",
            PW_KEY_MEDIA_CATEGORY, "Capture",
            PW_KEY_MEDIA_CLASS, "Audio/Source",
            PW_KEY_NODE_NAME, "ds5_mic",
            PW_KEY_NODE_DESCRIPTION, "DualSense BT Mic",
            PW_KEY_NODE_LATENCY, "512/48000",   // festes Quantum -> req planbar
            NULL),
        &src_stream_events, &d);
    uint8_t sbuf[1024];
    struct spa_pod_builder sbld = SPA_POD_BUILDER_INIT(sbuf, sizeof(sbuf));
    struct spa_audio_info_raw sinfo = {
        .format = SPA_AUDIO_FORMAT_F32, .rate = MIC_RATE, .channels = 1,
        .position = { SPA_AUDIO_CHANNEL_MONO },
    };
    const struct spa_pod *sparams[1];
    sparams[0] = spa_format_audio_raw_build(&sbld, SPA_PARAM_EnumFormat, &sinfo);
    pw_stream_connect(d.src_stream,
        PW_DIRECTION_OUTPUT,                // Source = liefert Audio (Mikrofon)
        PW_ID_ANY,
        PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS |
        PW_STREAM_FLAG_RT_PROCESS,
        sparams, 1);

    pthread_t mth;                          // Mic-Decode-Thread
    pthread_create(&mth, NULL, mic_thread, &d);
    pthread_detach(mth);

    fprintf(stderr, "DualSense BT Speaker + Mic als PipeWire-Knoten aktiv. Strg+C beendet.\n");
    pw_main_loop_run(d.loop);

    pw_stream_destroy(d.src_stream);
    pw_stream_destroy(d.stream);
    pw_main_loop_destroy(d.loop);
    opus_encoder_destroy(d.enc);
    close(d.hidraw_fd);
    pw_deinit();
    return 0;
}
