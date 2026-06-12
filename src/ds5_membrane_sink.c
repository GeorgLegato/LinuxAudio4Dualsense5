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

#define SRC_RATE        48000
#define CHANNELS        2
#define INPUT_BLOCK     512        // Frames pro Sende-Block (DS5_Bridge)
#define OPUS_FRAME      480        // Opus-Frame nach Resampling
#define OPUS_BITRATE    (200*8*100) // 160 kbps CBR -> exakt 200 byte/frame
#define SPEAKER_BYTES   200
#define REPORT_SIZE     398
#define STATE_SNAP_SIZE 63
#define FADE_SAMPLES    1920
#define PREROLL_PKTS    24

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

    float acc[INPUT_BLOCK * CHANNELS];   // Akkumulator fuer 512-Frame-Bloecke
    int acc_frames;
    int write_errors;                    // -> Controller weg -> Loop beenden
};

// --- 0x36-Paket bauen + senden ------------------------------------------------
static void send_block(struct data *d, const float *block512) {
    // Resample 512 -> 480 (linear), Fade-In, Float fuer opus_encode_float
    float out[OPUS_FRAME * CHANNELS];
    double step = (double)(INPUT_BLOCK - 1) / (OPUS_FRAME - 1);
    for (int i = 0; i < OPUS_FRAME; i++) {
        double src = i * step;
        int idx = (int)src;
        int nxt = idx < INPUT_BLOCK - 1 ? idx + 1 : idx;
        float frac = (float)(src - idx);
        for (int ch = 0; ch < CHANNELS; ch++) {
            float a = block512[idx*CHANNELS+ch];
            float b = block512[nxt*CHANNELS+ch];
            float v = a + (b - a) * frac;
            if (d->fade_pos < FADE_SAMPLES) {
                int s = d->fade_pos + i;
                if (s < FADE_SAMPLES) v *= (float)s / FADE_SAMPLES;
            }
            out[i*CHANNELS+ch] = v;
        }
    }
    if (d->fade_pos < FADE_SAMPLES) d->fade_pos += OPUS_FRAME;

    uint8_t opus[SPEAKER_BYTES];
    int n = opus_encode_float(d->enc, out, OPUS_FRAME, opus, SPEAKER_BYTES);
    if (n < 0) return;

    uint8_t pkt[REPORT_SIZE];
    memset(pkt, 0, sizeof(pkt));
    pkt[0] = 0x36;
    pkt[1] = (d->seq & 0x0F) << 4;
    pkt[2] = 0x11 | 0x80;  pkt[3] = 7;  pkt[4] = 0xFF;
    pkt[5]=pkt[6]=pkt[7]=pkt[8]=pkt[9] = 64;
    pkt[10] = d->counter;
    pkt[11] = 0x10 | 0x80; pkt[12] = STATE_SNAP_SIZE;
    memcpy(pkt + 13, STATE_SNAPSHOT, STATE_SNAP_SIZE);
    pkt[76] = 0x12 | 0x80; pkt[77] = 64;               // Haptik = Stille
    pkt[142] = 0x13 | 0x80; pkt[143] = SPEAKER_BYTES;
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
        uint32_t n_frames = n_bytes / (sizeof(float) * CHANNELS);
        for (uint32_t f = 0; f < n_frames; f++) {
            d->acc[d->acc_frames*CHANNELS]   = samples[f*CHANNELS];
            d->acc[d->acc_frames*CHANNELS+1] = samples[f*CHANNELS+1];
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
    fprintf(stderr, "DualSense: %s\n", hidraw);

    int err;
    d.enc = opus_encoder_create(SRC_RATE, CHANNELS, OPUS_APPLICATION_AUDIO, &err);
    if (err != OPUS_OK) { fprintf(stderr, "opus init: %s\n", opus_strerror(err)); return 1; }
    opus_encoder_ctl(d.enc, OPUS_SET_BITRATE(OPUS_BITRATE));
    opus_encoder_ctl(d.enc, OPUS_SET_VBR(0));            // CBR -> feste 200 byte
    opus_encoder_ctl(d.enc, OPUS_SET_COMPLEXITY(0));

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
        .channels = CHANNELS,
    };
    const struct spa_pod *params[1];
    params[0] = spa_format_audio_raw_build(&bld, SPA_PARAM_EnumFormat, &info);

    pw_stream_connect(d.stream,
        PW_DIRECTION_INPUT,                 // Sink = empfaengt Audio
        PW_ID_ANY,
        PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS |
        PW_STREAM_FLAG_RT_PROCESS,
        params, 1);

    fprintf(stderr, "DualSense BT Speaker als PipeWire-Sink aktiv. Strg+C beendet.\n");
    pw_main_loop_run(d.loop);

    pw_stream_destroy(d.stream);
    pw_main_loop_destroy(d.loop);
    opus_encoder_destroy(d.enc);
    close(d.hidraw_fd);
    pw_deinit();
    return 0;
}
