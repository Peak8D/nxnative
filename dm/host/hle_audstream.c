// nn::audio's Switch 2 streams (SDK 21; Star Fox: Wwise's sink). An output stream is a ring of frames in the system's sound mixer (sysmix, behind audsmx:u; started by attaching
// it to a virtual output device, auddmg:u): the game writes frames while the ring has free ones, and an optional data event is signalled once the free frames reach a threshold
// (OutputStreamImpl::Open's AcquireStreamDataEvent). The SDK's versions open those services; the host serves neither, and the game aborted at its audio setup (0xa99, sdk+0x221520).
// Host: each stream feeds the host PCM output (hle_audio.c: the AudioUnit, or the silent sink that consumes in real time) with one block per Write, converted to 16-bit with
// the stream's gain; a helper thread takes the played blocks back, which frees their frames, and signals the data event. Input streams: the host has no microphone, opens fail.
// Layouts (sdk OutputStreamImpl): StreamFormat {sample format, channel count, sample rate, ...} (16 bytes), the state 1 stopped, 2 started; statistics are three u64
#include "hle.h"
#include <stdatomic.h>

void *dm_pcmout_open(int ch); void dm_pcmout_close(void *k); void dm_pcmout_start(void *k); void dm_pcmout_stop(void *k);
int dm_pcmout_append(void *k, void *b); void *dm_pcmout_released(void *k); void dm_pcmout_wait(void *k, int ms);
typedef struct { void *ptr; uint64_t cap, size; } gbuf;   // hle_audio.c's block
enum { OS_STOPPED = 1, OS_STARTED = 2, SF_PCM16 = 2, SF_FLOAT = 5, OS_RATE = 48000 };
typedef struct { int32_t format, channels, rate, pad; } stream_format;
typedef struct ostream {
    pthread_mutex_t m;
    void *pcm, *ev;
    int fmt, ch, rate, cap, thresh, filled, id;
    _Atomic int state;
    float gain;
    gbuf *wait[64]; int nwait;   // blocks the host output's queue (64 deep) has no room for yet
    pthread_t th; _Atomic int run;
} ostream;
static ostream *os_of(const void *g) { ostream *s = objmap_get(g); if (!s) die("output stream %p was not opened by the host", g); return s; }
static int os_frames(const ostream *s, const gbuf *b) { return (int)(b->size / (2 * (uint64_t)s->ch)); }
static void os_flush(ostream *s) {   // under s->m: blocks waiting for room go to the output
    int k = 0;
    while (k < s->nwait && dm_pcmout_append(s->pcm, s->wait[k])) k++;
    memmove(s->wait, s->wait + k, (size_t)(s->nwait - k) * sizeof *s->wait); s->nwait -= k;
}
static int os_reap(ostream *s) {   // under s->m: played blocks free their frames
    int n = 0; gbuf *b;
    while ((b = dm_pcmout_released(s->pcm))) { s->filled -= os_frames(s, b); free(b->ptr); free(b); n++; }
    if (n) os_flush(s);
    return n;
}
static void os_signal(ostream *s) { if (s->ev && atomic_load(&s->state) == OS_STARTED && s->cap - s->filled >= s->thresh) hle_signal_system_event(s->ev); }
static void *os_thread(void *p) {
    ostream *s = p;
    while (atomic_load(&s->run)) {
        dm_pcmout_wait(s->pcm, 5);
        pthread_mutex_lock(&s->m); int n = os_reap(s); pthread_mutex_unlock(&s->m);
        if (n) os_signal(s);
    }
    return NULL;
}
static Result os_open(void *g, void *ev, const stream_format *f, int frames, int thresh) {
    static _Atomic int ids;
    ostream *s = calloc(1, sizeof *s);
    pthread_mutex_init(&s->m, NULL);
    s->fmt = f->format; s->ch = f->channels; s->rate = f->rate; s->cap = frames; s->thresh = thresh > 0 ? thresh : 1; s->gain = 1.f; s->id = ++ids;
    atomic_store(&s->state, OS_STOPPED);
    if (s->ch != 2 && s->ch != 6) LOG("[RED] output stream #%d: %d channels, the host output takes 2 or 6", s->id, s->ch);
    if (s->rate != OS_RATE) LOG("[RED] output stream #%d: %d Hz, the host output plays 48 kHz (not resampled)", s->id, s->rate);
    if (s->fmt != SF_PCM16 && s->fmt != SF_FLOAT) LOG("[RED] output stream #%d: sample format %d, read as 16-bit", s->id, s->fmt);
    s->pcm = dm_pcmout_open(s->ch == 6 ? 6 : 2);
    if (ev) { s->ev = ev; hle_bind_system_event(ev, 1); }
    atomic_store(&s->run, 1);
    if (pthread_create(&s->th, NULL, os_thread, s)) die("output stream: cannot start its thread");
    objmap_put(g, s);
    LOG("audio: output stream #%d, format %d, %d channel(s), %d Hz, %d frames%s (data event at %d free)", s->id, s->fmt, s->ch, s->rate, s->cap, ev ? "" : ", no event", s->thresh);
    return RES_OK;
}
HLE_FORCE_BIG("_ZN2nn5audio16OpenOutputStreamEPNS0_16OutputStreamTypeEPNS_2os11SystemEventEPvmRKNS0_15AudioDeviceNameERKNS0_12StreamFormatEiiNS_4util10BitFlagSetILi32ENS0_16OutputStreamFlagEEE",
          "sysmix / auddmg services; a host PCM output with a data event", Result, h_os_OpenEv,
          void *g, void *ev, void *buf, size_t size, const char *dev, const stream_format *f, int frames, int thresh, uint64_t flags) {
    (void)buf; (void)size; (void)dev; (void)flags; return os_open(g, ev, f, frames, thresh);
}
HLE_FORCE_BIG("_ZN2nn5audio16OpenOutputStreamEPNS0_16OutputStreamTypeEPvmRKNS0_15AudioDeviceNameERKNS0_12StreamFormatEiNS_4util10BitFlagSetILi32ENS0_16OutputStreamFlagEEE",
          "sysmix / auddmg services; a host PCM output", Result, h_os_Open, void *g, void *buf, size_t size, const char *dev, const stream_format *f, int frames, uint64_t flags) {
    (void)buf; (void)size; (void)dev; (void)flags; return os_open(g, NULL, f, frames, 0);
}
static void os_start(ostream *s) {
    if (atomic_exchange(&s->state, OS_STARTED) != OS_STARTED) dm_pcmout_start(s->pcm);
    os_signal(s);
}
static void os_stop(ostream *s) {   // the frames not yet played are dropped (the host output releases them)
    if (atomic_exchange(&s->state, OS_STOPPED) != OS_STARTED) return;
    dm_pcmout_stop(s->pcm);
    pthread_mutex_lock(&s->m); os_reap(s); pthread_mutex_unlock(&s->m);
}
HLE_FORCE("_ZN2nn5audio17StartOutputStreamEPNS0_16OutputStreamTypeE", "attaches the stream to the device; starts the host output", Result, h_os_Start, void *g) { os_start(os_of(g)); return RES_OK; }
HLE_FORCE("_ZN2nn5audio18StartOutputStreamsEPKPNS0_16OutputStreamTypeEi", "starts several streams together", Result, h_os_StartN, void *const *g, int n) {
    for (int i = 0; i < n; i++) os_start(os_of(g[i]));
    return RES_OK;
}
HLE_FORCE("_ZN2nn5audio16StopOutputStreamEPNS0_16OutputStreamTypeE", "detaches the stream; stops the host output", void, h_os_Stop, void *g) { os_stop(os_of(g)); }
HLE_FORCE("_ZN2nn5audio17PurgeOutputStreamEPNS0_16OutputStreamTypeE", "drops the frames written and not yet played", void, h_os_Purge, void *g) {
    ostream *s = os_of(g); int was = atomic_load(&s->state);
    os_stop(s);
    pthread_mutex_lock(&s->m); for (int i = 0; i < s->nwait; i++) { s->filled -= os_frames(s, s->wait[i]); free(s->wait[i]->ptr); free(s->wait[i]); } s->nwait = 0; pthread_mutex_unlock(&s->m);
    if (was == OS_STARTED) os_start(s);
}
HLE_FORCE_BIG("_ZN2nn5audio17CloseOutputStreamEPNS0_16OutputStreamTypeE", "closes the stream and its host output", void, h_os_Close, void *g) {
    ostream *s = objmap_del(g); if (!s) return;
    os_stop(s);
    atomic_store(&s->run, 0); pthread_join(s->th, NULL);
    dm_pcmout_close(s->pcm);
    for (int i = 0; i < s->nwait; i++) { free(s->wait[i]->ptr); free(s->wait[i]); }
    pthread_mutex_destroy(&s->m); free(s);
}
HLE_FORCE("_ZN2nn5audio17WriteOutputStreamEPNS0_16OutputStreamTypeEPKvi", "frames into the stream's ring; a block for the host output", int, h_os_Write, void *g, const void *data, int frames) {
    ostream *s = os_of(g);
    pthread_mutex_lock(&s->m);
    os_reap(s);
    int n = frames < s->cap - s->filled ? frames : s->cap - s->filled;
    if (n <= 0 || s->nwait == 64) { pthread_mutex_unlock(&s->m); return 0; }
    int och = s->ch == 6 ? 6 : 2, ich = s->ch; size_t ns = (size_t)n * och;
    gbuf *b = malloc(sizeof *b); int16_t *o = malloc(ns * 2);
    for (int i = 0; i < n; i++)
        for (int c = 0; c < och; c++) {
            float v = c < ich ? (s->fmt == SF_FLOAT ? ((const float *)data)[(size_t)i * ich + c] * 32767.f : ((const int16_t *)data)[(size_t)i * ich + c]) * s->gain : 0.f;
            o[(size_t)i * och + c] = (int16_t)(v > 32767.f ? 32767.f : v < -32768.f ? -32768.f : v);
        }
    b->ptr = o; b->cap = b->size = ns * 2;
    s->wait[s->nwait++] = b; s->filled += n; os_flush(s);
    pthread_mutex_unlock(&s->m);
    return n;
}
HLE_FORCE("_ZN2nn5audio29GetOutputStreamFreeFrameCountEPKNS0_16OutputStreamTypeE", "free frames of the stream's ring", int, h_os_Free, const void *g) {
    ostream *s = os_of(g); pthread_mutex_lock(&s->m); os_reap(s); int n = s->cap - s->filled; pthread_mutex_unlock(&s->m); return n;
}
HLE_FORCE("_ZN2nn5audio31GetOutputStreamFilledFrameCountEPKNS0_16OutputStreamTypeE", "frames written and not yet played", int, h_os_Filled, const void *g) {
    ostream *s = os_of(g); pthread_mutex_lock(&s->m); os_reap(s); int n = s->filled; pthread_mutex_unlock(&s->m); return n;
}
// A stream never opened is 0 (closed), as the sdk answers: Star Fox asks after its second device's stream (main+0xbce927c) when it has opened only the first
HLE_FORCE("_ZN2nn5audio20GetOutputStreamStateEPKNS0_16OutputStreamTypeE", "0 closed, 1 stopped, 2 started", int, h_os_State, const void *g) { ostream *s = objmap_get(g); return s ? atomic_load(&s->state) : 0; }
HLE_FORCE("_ZN2nn5audio19SetOutputStreamGainEPNS0_16OutputStreamTypeEfNS_8TimeSpanE", "the stream's gain, applied to the frames written from now (the fade time is not kept)", void, h_os_Gain,
          void *g, float gain, int64_t fade) {
    (void)fade; ostream *s = os_of(g); pthread_mutex_lock(&s->m); s->gain = gain >= 0.f ? gain : 0.f; pthread_mutex_unlock(&s->m);
}
typedef struct { uint64_t v[3]; } os_stats;
HLE_FORCE("_ZN2nn5audio25GetOutputStreamStatisticsEPNS0_16OutputStreamTypeEb", "the mixer's stream statistics; none kept", os_stats, h_os_Stats, void *g, int reset) {
    (void)os_of(g); (void)reset; os_stats z = {{0}}; return z;
}
HLE_FORCE("_ZN2nn5audio29GetDefaultVirtualOutputDeviceEPNS0_15AudioDeviceNameE", "auddmg service; the host's one output", Result, h_os_DefDev, char *name) {
    memset(name, 0, 0x100); strcpy(name, "AudioVirtualOutput0"); return RES_OK;
}

// Input streams (a microphone; GameChat's): the host has none. No device is listed and an open fails, as the AudioIn functions in hle_audio.c
HLE_FORCE("_ZN2nn5audio23ListVirtualInputDevicesEPNS0_15AudioDeviceNameEi", "auddmg service; no microphone: 0 devices", int, h_is_List, void *names, int n) { (void)names; (void)n; return 0; }
HLE_FORCE("_ZN2nn5audio31GetVirtualInputDevicePropertiesEPNS0_23VirtualDevicePropertiesERKNS0_15AudioDeviceNameE", "auddmg service; no microphone: fails", Result, h_is_Props,
          void *p, const void *name) { (void)p; (void)name; return RES_FAIL; }
HLE_FORCE("_ZN2nn5audio15OpenInputStreamEPNS0_15InputStreamTypeEPvmRKNS0_15AudioDeviceNameERKNS0_12StreamFormatEi", "sysmix service; no microphone: fails", Result, h_is_Open,
          void *g, void *buf, size_t size, const void *name, const void *f, int frames) {
    (void)g; (void)buf; (void)size; (void)name; (void)f; (void)frames;
    static _Atomic int once; if (!atomic_exchange(&once, 1)) LOG("audio: an input stream: the host has no microphone, the open fails");
    return RES_FAIL;
}
HLE_FORCE("_ZN2nn5audio16StartInputStreamEPNS0_15InputStreamTypeE", "never opened on the host: fails", Result, h_is_Start, void *g) { (void)g; return RES_FAIL; }
HLE_FORCE("_ZN2nn5audio15StopInputStreamEPNS0_15InputStreamTypeE", "never opened on the host", void, h_is_Stop, void *g) { (void)g; }
HLE_FORCE("_ZN2nn5audio16CloseInputStreamEPNS0_15InputStreamTypeE", "never opened on the host", void, h_is_Close, void *g) { (void)g; }
HLE_FORCE("_ZN2nn5audio15ReadInputStreamEPNS0_15InputStreamTypeEPvi", "never opened on the host: no frames", int, h_is_Read, void *g, void *buf, int frames) { (void)g; (void)buf; (void)frames; return 0; }
HLE_FORCE("_ZN2nn5audio30GetInputStreamFilledFrameCountEPKNS0_15InputStreamTypeE", "never opened on the host: no frames", int, h_is_Filled, const void *g) { (void)g; return 0; }
HLE_FORCE("_ZN2nn5audio20GetInputStreamFormatEPKNS0_15InputStreamTypeE", "never opened on the host: a zero format", stream_format, h_is_Format, const void *g) {
    (void)g; stream_format z = {0, 0, 0, 0}; return z;
}
