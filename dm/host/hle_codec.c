// nn::codec::HardwareOpusDecoder: the real device decodes in hardware via the hwopus service (the game's audio and cutscene tracks are all Opus; this is not an online feature).
// Host: the constructor instead constructs the SDK's software OpusDecoder in the same memory (the sdk bundles libopus, native ARM).
// Basis: both are implementations of nn::codec::IOpusDecoder, with the 10 vtable slots identical one by one in order and signature (destructor×2, GetWorkBufferSize, IsInitialized,
// Initialize, Finalize, GetSampleRate, GetChannelCount, Reset, DecodeInterleaved); the game allocates 0x40 bytes and the software object is 0x30 bytes.
#include "hle.h"

HLE_FORCE("_ZN2nn5codec19HardwareOpusDecoderC1Ei", "hwopus service; replaced by the SDK software OpusDecoder (same IOpusDecoder virtual interface)",
          void, h_codec_HwOpusCtor, void *self, int option) {
    (void)option;
    static void (*soft_ctor)(void *);
    if (!soft_ctor && !(soft_ctor = guest_sym("_ZN2nn5codec11OpusDecoderC1Ev"))) die("missing the OpusDecoder constructor");
    soft_ctor(self);
}

// The C interface to the same hardware decoder (Tears of the Kingdom, SDK 20.5.22: GetHardwareOpusDecoderWorkBufferSize opens hwopus and aborts in sm::Initialize).
// Each call goes to the sdk's software C interface instead. The software decoder state lives in a host block kept per game decoder object (objmap), so the size of the
// game's HardwareOpusDecoderType does not matter; the game's work buffer is sized by GetOpusDecoderWorkBufferSize and handed to the software decoder as is.
#define SOFT_OPUS_STATE 0x200   // bytes for the software OpusDecoderType (larger than the SDK's)
static void *opus_fn(const char *name) { void *f = guest_sym(name); if (!f) die("sdk lacks %s", name); return f; }
static void *opus_soft(const void *hw) { void *s = objmap_get(hw); if (!s) die("Opus decoder %p was not initialized by the host", hw); return s; }
static void opus_q_drop(void *hw, int fini);   // a batch decoder's queue (InitializeHardwareOpusDecoder with a manager, below)
HLE_FORCE("_ZN2nn5codec36GetHardwareOpusDecoderWorkBufferSizeEiii", "hwopus service; work buffer computed for the software decoder", size_t, h_codec_HwOpusWorkSize, int rate, int ch, int option) {
    (void)option; size_t (*f)(int, int) = opus_fn("_ZN2nn5codec28GetOpusDecoderWorkBufferSizeEii"); return f(rate, ch);
}
// SDK 11's form without the option (Big Brain Academy: Brain vs. Brain sizes its decoders with it early in nnMain, main+0x31f8e8; the SDK's own opens hwopus and aborts resolving the service)
HLE_FORCE("_ZN2nn5codec36GetHardwareOpusDecoderWorkBufferSizeEii", "hwopus service; work buffer computed for the software decoder (SDK 11 two-argument version)", size_t, h_codec_HwOpusWorkSize2,
          int rate, int ch) {
    size_t (*f)(int, int) = opus_fn("_ZN2nn5codec28GetOpusDecoderWorkBufferSizeEii"); return f(rate, ch);
}
HLE_FORCE("_ZN2nn5codec29InitializeHardwareOpusDecoderEPNS0_23HardwareOpusDecoderTypeEiiPvmi", "hwopus service; replaced by the software OpusDecoder", int, h_codec_HwOpusInit,
          void *hw, int rate, int ch, void *buf, size_t size, int option) {
    (void)option; int (*f)(void *, int, int, void *, size_t) = opus_fn("_ZN2nn5codec21InitializeOpusDecoderEPNS0_15OpusDecoderTypeEiiPvm");
    void *s = objmap_get(hw); if (!s) { s = calloc(1, SOFT_OPUS_STATE); objmap_put(hw, s); }
    return f(s, rate, ch, buf, size);
}
HLE_FORCE("_ZN2nn5codec27FinalizeHardwareOpusDecoderEPNS0_23HardwareOpusDecoderTypeE", "hwopus service; software OpusDecoder", void, h_codec_HwOpusFini, void *hw) {
    void (*f)(void *) = opus_fn("_ZN2nn5codec19FinalizeOpusDecoderEPNS0_15OpusDecoderTypeE");
    opus_q_drop(hw, 1); void *s = objmap_del(hw); if (s) { f(s); free(s); }
}
HLE_FORCE("_ZN2nn5codec32GetHardwareOpusDecoderSampleRateEPKNS0_23HardwareOpusDecoderTypeE", "hwopus service; software OpusDecoder", int, h_codec_HwOpusRate, const void *hw) {
    int (*f)(const void *) = opus_fn("_ZN2nn5codec24GetOpusDecoderSampleRateEPKNS0_15OpusDecoderTypeE"); return f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec34GetHardwareOpusDecoderChannelCountEPKNS0_23HardwareOpusDecoderTypeE", "hwopus service; software OpusDecoder", int, h_codec_HwOpusCh, const void *hw) {
    int (*f)(const void *) = opus_fn("_ZN2nn5codec26GetOpusDecoderChannelCountEPKNS0_15OpusDecoderTypeE"); return f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec24ResetHardwareOpusDecoderEPNS0_23HardwareOpusDecoderTypeE", "hwopus service; software OpusDecoder", void, h_codec_HwOpusReset, void *hw) {
    void (*f)(void *) = opus_fn("_ZN2nn5codec16ResetOpusDecoderEPNS0_15OpusDecoderTypeE"); opus_q_drop(hw, 0); f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec33DecodeOpusInterleavedWithHardwareEPNS0_23HardwareOpusDecoderTypeEPmPiPsmPKvm", "hwopus service; software OpusDecoder", int, h_codec_HwOpusDecode,
          void *hw, size_t *consumed, int *samples, int16_t *out, size_t out_size, const void *in, size_t in_size) {
    int (*f)(void *, size_t *, int *, int16_t *, size_t, const void *, size_t) = opus_fn("_ZN2nn5codec21DecodeOpusInterleavedEPNS0_15OpusDecoderTypeEPmPiPsmPKvm");
    return f(opus_soft(hw), consumed, samples, out, out_size, in, in_size);
}

// The Switch 2 batch interface to the same decoder (Star Fox, SDK 21; sdk nn::codec::opus::hw over hwacod): a decoder manager with a number of I/O buffers and a system event.
// A decoder queues one Opus packet per Enqueue (its 8-byte header, then the packet: consumed = its length + 8, hwacod::shim::GetFirstInputDataSize), the manager decodes
// everything queued (Execute) and signals its event, and Dequeue takes the oldest decoded packet's PCM (samples = bytes / (2 × channels)). Unhooked, the manager's
// initialization opened hwopus and the sdk aborted (0xe15, sm: not registered). Host: each decoder is the software OpusDecoder as above (state in objmap, so Reset and
// Finalize above apply), its packets in a host queue decoded on the caller's thread at Execute; the event is bound at the manager's initialization, signalled after each Execute
#include <pthread.h>
#define OPUS_MAX_FRAME 5760   // samples per channel in Opus's longest frame (120 ms at 48 kHz)
typedef struct opus_req { struct opus_req *next; uint8_t *in; size_t in_size; int16_t *pcm; size_t pcm_bytes; int res, done; } opus_req;
typedef struct opus_mgr { void *ev; int nio, used; struct opus_q *decs; } opus_mgr;
typedef struct opus_q { struct opus_q *next; void *hw; opus_mgr *mgr; int ch; opus_req *head; void *work; } opus_q;   // in objmap at (char *)decoder + 1
static pthread_mutex_t g_opus_mu = PTHREAD_MUTEX_INITIALIZER;   // queues and counts: the game's audio threads enqueue, execute and dequeue
static opus_q *opus_q_of(const void *hw) { return objmap_get((const char *)hw + 1); }
static void opus_q_drop(void *hw, int fini) {
    pthread_mutex_lock(&g_opus_mu);
    opus_q *q = fini ? objmap_del((char *)hw + 1) : opus_q_of(hw);
    if (q) {
        for (opus_req *r = q->head, *n; r; r = n) { n = r->next; free(r->in); free(r->pcm); free(r); q->mgr->used--; }
        q->head = NULL;
        if (fini) { for (opus_q **p = &q->mgr->decs; *p; p = &(*p)->next) if (*p == q) { *p = q->next; break; } free(q->work); free(q); }
    }
    pthread_mutex_unlock(&g_opus_mu);
}
HLE_FORCE("_ZN2nn5codec43GetHardwareOpusDecoderManagerWorkBufferSizeEiiNS_8TimeSpanEii", "hwopus service; the host keeps the manager's state itself", size_t, h_codec_HwOpusMgrSize,
          int rate, int ch, int64_t frame, int ndec, int nio) { (void)rate; (void)ch; (void)frame; (void)ndec; (void)nio; return 0x1000; }
HLE_FORCE("_ZN2nn5codec36InitializeHardwareOpusDecoderManagerEPNS0_30HardwareOpusDecoderManagerTypeEPNS_2os15SystemEventTypeEiiNS_8TimeSpanEiiPvm",
          "hwopus service; the software decoders' manager, its event bound to a kernel event", int, h_codec_HwOpusMgrInit,
          void *mgr, void *ev, int rate, int ch, int64_t frame, int ndec, int nio, void *buf, size_t size) {
    (void)rate; (void)ch; (void)frame; (void)buf; (void)size;
    opus_mgr *m = calloc(1, sizeof *m); m->ev = ev; m->nio = nio;
    hle_bind_system_event(ev, 1); objmap_put(mgr, m);
    LOG("codec: hardware Opus decoder manager, %d decoder(s), %d I/O buffer(s): the software decoder", ndec, nio);
    return RES_OK;
}
HLE_FORCE("_ZN2nn5codec34FinalizeHardwareOpusDecoderManagerEPNS0_30HardwareOpusDecoderManagerTypeE", "hwopus service; software decoders' manager", void, h_codec_HwOpusMgrFini, void *mgr) {
    pthread_mutex_lock(&g_opus_mu); opus_mgr *m = objmap_del(mgr); if (m && !m->decs) free(m); pthread_mutex_unlock(&g_opus_mu);   // with decoders left, they keep it
}
HLE_FORCE("_ZN2nn5codec29InitializeHardwareOpusDecoderEPNS0_23HardwareOpusDecoderTypeEiiNS_8TimeSpanEPNS0_30HardwareOpusDecoderManagerTypeE",
          "hwopus service; replaced by the software OpusDecoder with a host queue", int, h_codec_HwOpusInitMgr, void *hw, int rate, int ch, int64_t frame, void *mgr) {
    (void)frame;
    opus_mgr *m = objmap_get(mgr); if (!m) die("Opus decoder %p: manager %p was not initialized by the host", hw, mgr);
    size_t (*wsize)(int, int) = opus_fn("_ZN2nn5codec28GetOpusDecoderWorkBufferSizeEii");
    int (*init)(void *, int, int, void *, size_t) = opus_fn("_ZN2nn5codec21InitializeOpusDecoderEPNS0_15OpusDecoderTypeEiiPvm");
    size_t n = wsize(rate, ch); void *work = calloc(1, n);
    void *s = objmap_get(hw); if (!s) { s = calloc(1, SOFT_OPUS_STATE); objmap_put(hw, s); }
    int r = init(s, rate, ch, work, n);
    if (r) { free(work); return r; }
    opus_q *q = calloc(1, sizeof *q); q->hw = hw; q->mgr = m; q->ch = ch; q->work = work;
    pthread_mutex_lock(&g_opus_mu); q->next = m->decs; m->decs = q; objmap_put((char *)hw + 1, q); pthread_mutex_unlock(&g_opus_mu);
    return RES_OK;
}
HLE_FORCE("_ZN2nn5codec36EnqueueOpusDataToHardwareOpusDecoderEPNS0_23HardwareOpusDecoderTypeEPmPKvm", "hwopus service; queued for the software decoder", int, h_codec_HwOpusEnqueue,
          void *hw, size_t *consumed, const void *data, size_t size) {
    opus_q *q = opus_q_of(hw); if (!q) die("Opus decoder %p has no manager", hw);
    const uint8_t *d = data;
    if (size < 8 || 8 + (size_t)(d[0] << 24 | d[1] << 16 | d[2] << 8 | d[3]) > size) { static int once; if (!once++) LOG("[RED] codec: an Opus packet of %zu bytes whose header does not fit", size); return 515183; }   // hwacod's own answer
    size_t n = 8 + (size_t)(d[0] << 24 | d[1] << 16 | d[2] << 8 | d[3]);
    pthread_mutex_lock(&g_opus_mu);
    if (q->mgr->used >= q->mgr->nio) { pthread_mutex_unlock(&g_opus_mu); return 14; }   // the sdk's answer with no free I/O buffer
    opus_req *r = calloc(1, sizeof *r); r->in = malloc(n); memcpy(r->in, data, n); r->in_size = n;
    opus_req **t = &q->head; while (*t) t = &(*t)->next; *t = r; q->mgr->used++;
    pthread_mutex_unlock(&g_opus_mu);
    *consumed = n; return RES_OK;
}
HLE_FORCE("_ZN2nn5codec34ExecuteHardwareOpusDecoderDecodingEPNS0_30HardwareOpusDecoderManagerTypeE", "hwopus service; the queued packets decoded by the software decoder", int, h_codec_HwOpusExecute,
          void *mgr) {
    opus_mgr *m = objmap_get(mgr); if (!m) die("Opus decoder manager %p was not initialized by the host", mgr);
    int (*dec)(void *, size_t *, int *, int16_t *, size_t, const void *, size_t) = opus_fn("_ZN2nn5codec21DecodeOpusInterleavedEPNS0_15OpusDecoderTypeEPmPiPsmPKvm");
    pthread_mutex_lock(&g_opus_mu);
    for (opus_q *q = m->decs; q; q = q->next)
        for (opus_req *r = q->head; r; r = r->next) {
            if (r->done) continue;
            size_t cap = (size_t)OPUS_MAX_FRAME * q->ch * 2, used = 0; int samples = 0;
            r->pcm = malloc(cap);
            r->res = dec(opus_soft(q->hw), &used, &samples, r->pcm, cap, r->in, r->in_size);
            r->pcm_bytes = r->res ? 0 : (size_t)samples * q->ch * 2; r->done = 1;
            free(r->in); r->in = NULL;
        }
    pthread_mutex_unlock(&g_opus_mu);
    hle_signal_system_event(m->ev);
    return RES_OK;
}
HLE_FORCE("_ZN2nn5codec48DequeuePcmInterleavedDataFromHardwareOpusDecoderEPNS0_23HardwareOpusDecoderTypeEPiPsm", "hwopus service; the software decoder's oldest decoded packet",
          int, h_codec_HwOpusDequeue, void *hw, int *samples, int16_t *pcm, size_t pcm_size) {
    opus_q *q = opus_q_of(hw); if (!q) die("Opus decoder %p has no manager", hw);
    pthread_mutex_lock(&g_opus_mu);
    opus_req *r = q->head; *samples = 0;
    if (!r || !r->done) { pthread_mutex_unlock(&g_opus_mu); return RES_OK; }   // nothing decoded yet: no samples
    size_t n = r->pcm_bytes < pcm_size ? r->pcm_bytes : pcm_size; memcpy(pcm, r->pcm, n);
    *samples = (int)(n / (2 * (size_t)q->ch)); int res = r->res;
    q->head = r->next; q->mgr->used--; free(r->pcm); free(r);
    pthread_mutex_unlock(&g_opus_mu);
    return res;
}
static int opus_count(const void *hw, int done) {
    opus_q *q = opus_q_of(hw); int n = 0;
    pthread_mutex_lock(&g_opus_mu); if (q) for (opus_req *r = q->head; r; r = r->next) n += r->done == done; pthread_mutex_unlock(&g_opus_mu);
    return n;
}
HLE_FORCE("_ZN2nn5codec47GetHardwareOpusDecoderWaitingDecodeRequestCountEPKNS0_23HardwareOpusDecoderTypeE", "hwopus service; packets queued, not decoded", int, h_codec_HwOpusWaiting,
          const void *hw) { return opus_count(hw, 0); }
HLE_FORCE("_ZN2nn5codec49GetHardwareOpusDecoderExecutingDecodeRequestCountEPKNS0_23HardwareOpusDecoderTypeE", "hwopus service; Execute decodes before it returns: none",
          int, h_codec_HwOpusExecuting, const void *hw) { (void)hw; return 0; }
HLE_FORCE("_ZN2nn5codec49GetHardwareOpusDecoderCompletedDecodeRequestCountEPKNS0_23HardwareOpusDecoderTypeE", "hwopus service; packets decoded, not dequeued", int, h_codec_HwOpusCompleted,
          const void *hw) { return opus_count(hw, 1); }
HLE_FORCE("_ZN2nn5codec46GetHardwareOpusDecoderManagerFreeIoBufferCountEPKNS0_30HardwareOpusDecoderManagerTypeE", "hwopus service; the manager's I/O buffers not holding a packet",
          int, h_codec_HwOpusFreeIo, const void *mgr) {
    opus_mgr *m = objmap_get(mgr); if (!m) return 0;
    pthread_mutex_lock(&g_opus_mu); int n = m->nio - m->used; pthread_mutex_unlock(&g_opus_mu); return n;
}

// nn::codec::HardwareOpusMultiStreamDecoder (Hyrule Warriors: Age of Calamity, SDK 9.6.0: its sound streams are multi-stream Opus): the same swap as HardwareOpusDecoder above.
// Both classes implement one interface: their 12 vtable slots match one by one (destructor×2, GetWorkBufferSize, IsInitialized, Initialize, Finalize, GetSampleRate, GetChannelCount,
// GetTotalStreamCount, GetStereoStreamCount, Reset, DecodeInterleaved); the hardware object is 0x48 bytes, the software one 0x38. Both destructors are a bare ret.
// Without it the game's KIDS resource loader never finished the first sound stream (asset/data/0x272c6efb.file) and the main thread waited for it forever (main+0x8d9bc8)
HLE_FORCE("_ZN2nn5codec30HardwareOpusMultiStreamDecoderC1Ei", "hwopus service; replaced by the SDK software OpusMultiStreamDecoder (same virtual interface)",
          void, h_codec_HwOpusMsCtor, void *self, int option) {
    (void)option; void (*f)(void *) = opus_fn("_ZN2nn5codec22OpusMultiStreamDecoderC1Ev"); f(self);
}
HLE_FORCE("_ZN2nn5codec47GetHardwareOpusMultiStreamDecoderWorkBufferSizeEiiii", "hwopus service; work buffer computed for the software decoder", size_t, h_codec_HwOpusMsWorkSize,
          int rate, int ch, int streams, int stereo) {
    size_t (*f)(int, int, int, int) = opus_fn("_ZN2nn5codec39GetOpusMultiStreamDecoderWorkBufferSizeEiiii"); return f(rate, ch, streams, stereo);
}

// The C interface to the hardware multi-stream decoder (Unicorn Overlord, SDK 16.2.4: CRI's Opus streams; GetHardwareOpusMultiStreamDecoderWorkBufferSize opened hwopus and the
// game aborted in sm::Initialize at nnMain). The same swap as the single-stream C interface above: software state per game decoder object, the work buffer sized for the software decoder.
// The software OpusMultiStreamDecoderType is 0x28 bytes (InitializeOpusMultiStreamDecoder writes up to +0x24), well inside SOFT_OPUS_STATE.
HLE_FORCE("_ZN2nn5codec47GetHardwareOpusMultiStreamDecoderWorkBufferSizeEiiiii", "hwopus service; work buffer computed for the software decoder", size_t, h_codec_HwOpusMsWorkSize5,
          int rate, int ch, int streams, int stereo, int option) {
    (void)option; size_t (*f)(int, int, int, int) = opus_fn("_ZN2nn5codec39GetOpusMultiStreamDecoderWorkBufferSizeEiiii"); return f(rate, ch, streams, stereo);
}
HLE_FORCE("_ZN2nn5codec40InitializeHardwareOpusMultiStreamDecoderEPNS0_34HardwareOpusMultiStreamDecoderTypeEiiiiPKhPvmi", "hwopus service; replaced by the software OpusMultiStreamDecoder",
          int, h_codec_HwOpusMsInit, void *hw, int rate, int ch, int streams, int stereo, const uint8_t *mapping, void *buf, size_t size, int option) {
    (void)option; int (*f)(void *, int, int, int, int, const uint8_t *, void *, size_t) = opus_fn("_ZN2nn5codec32InitializeOpusMultiStreamDecoderEPNS0_26OpusMultiStreamDecoderTypeEiiiiPKhPvm");
    void *s = objmap_get(hw); if (!s) { s = calloc(1, SOFT_OPUS_STATE); objmap_put(hw, s); }
    return f(s, rate, ch, streams, stereo, mapping, buf, size);
}
HLE_FORCE("_ZN2nn5codec38FinalizeHardwareOpusMultiStreamDecoderEPNS0_34HardwareOpusMultiStreamDecoderTypeE", "hwopus service; software OpusMultiStreamDecoder", void, h_codec_HwOpusMsFini, void *hw) {
    void (*f)(void *) = opus_fn("_ZN2nn5codec30FinalizeOpusMultiStreamDecoderEPNS0_26OpusMultiStreamDecoderTypeE");
    void *s = objmap_del(hw); if (s) { f(s); free(s); }
}
HLE_FORCE("_ZN2nn5codec43GetHardwareOpusMultiStreamDecoderSampleRateEPKNS0_34HardwareOpusMultiStreamDecoderTypeE", "hwopus service; software OpusMultiStreamDecoder", int, h_codec_HwOpusMsRate, const void *hw) {
    int (*f)(const void *) = opus_fn("_ZN2nn5codec35GetOpusMultiStreamDecoderSampleRateEPKNS0_26OpusMultiStreamDecoderTypeE"); return f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec45GetHardwareOpusMultiStreamDecoderChannelCountEPKNS0_34HardwareOpusMultiStreamDecoderTypeE", "hwopus service; software OpusMultiStreamDecoder", int, h_codec_HwOpusMsCh, const void *hw) {
    int (*f)(const void *) = opus_fn("_ZN2nn5codec37GetOpusMultiStreamDecoderChannelCountEPKNS0_26OpusMultiStreamDecoderTypeE"); return f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec49GetHardwareOpusMultiStreamDecoderTotalStreamCountEPKNS0_34HardwareOpusMultiStreamDecoderTypeE", "hwopus service; software OpusMultiStreamDecoder", int, h_codec_HwOpusMsTotal, const void *hw) {
    int (*f)(const void *) = opus_fn("_ZN2nn5codec41GetOpusMultiStreamDecoderTotalStreamCountEPKNS0_26OpusMultiStreamDecoderTypeE"); return f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec50GetHardwareOpusMultiStreamDecoderStereoStreamCountEPKNS0_34HardwareOpusMultiStreamDecoderTypeE", "hwopus service; software OpusMultiStreamDecoder", int, h_codec_HwOpusMsStereo, const void *hw) {
    int (*f)(const void *) = opus_fn("_ZN2nn5codec42GetOpusMultiStreamDecoderStereoStreamCountEPKNS0_26OpusMultiStreamDecoderTypeE"); return f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec35ResetHardwareOpusMultiStreamDecoderEPNS0_34HardwareOpusMultiStreamDecoderTypeE", "hwopus service; software OpusMultiStreamDecoder", void, h_codec_HwOpusMsReset, void *hw) {
    void (*f)(void *) = opus_fn("_ZN2nn5codec27ResetOpusMultiStreamDecoderEPNS0_26OpusMultiStreamDecoderTypeE"); f(opus_soft(hw));
}
HLE_FORCE("_ZN2nn5codec44DecodeOpusMultiStreamInterleavedWithHardwareEPNS0_34HardwareOpusMultiStreamDecoderTypeEPmPiPsmPKvm", "hwopus service; software OpusMultiStreamDecoder", int, h_codec_HwOpusMsDecode,
          void *hw, size_t *consumed, int *samples, int16_t *out, size_t out_size, const void *in, size_t in_size) {
    int (*f)(void *, size_t *, int *, int16_t *, size_t, const void *, size_t) = opus_fn("_ZN2nn5codec32DecodeOpusMultiStreamInterleavedEPNS0_26OpusMultiStreamDecoderTypeEPmPiPsmPKvm");
    return f(opus_soft(hw), consumed, samples, out, out_size, in, in_size);
}
// The hardware's decode time of the last frame (nn::TimeSpan); the software decoder runs inline on the caller's thread, so none is reported
HLE_FORCE("_ZN2nn5codec47GetHardwareOpusMultiStreamDecoderProcessingTimeEPKNS0_34HardwareOpusMultiStreamDecoderTypeE", "hwopus service; no hardware decode time", int64_t, h_codec_HwOpusMsTime, const void *hw) {
    (void)hw; return 0;
}
