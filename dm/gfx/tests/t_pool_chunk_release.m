// A large pool's chunk that only mtl_addr_mapped reached (never resident) stays alive through its deferred release when it is lengthened or consolidated.
#define nvn_game_fps nvn_game_fps_mtl_default
#include "../nvn/nvn_mtl.m"
#undef nvn_game_fps
uint64_t g_t0_ns;
void *front_metal_layer(void) { return NULL; }
module_t *module_of(const void *p) { (void)p; return NULL; }
module_t *g_mod; int g_nmod;
void svc_thread_dump(void) {}
uint64_t now_ns(void) { return bp_now(); }
static int checks, failed;
#define CHECK(c, s) do { checks++; if (!(c)) { failed++; fprintf(stderr, "FAIL: %s\n", s); } } while (0)
static int queued(id o) { int n = 0; pthread_mutex_lock(&g_deferred_mu); for (NvDeferredRelease *d in g_deferred) n += d->object == o; pthread_mutex_unlock(&g_deferred_mu); return n; }
static __weak id chunk_of(uint8_t *p, uint64_t n) {   // the chunk entry a lookup of [p, p + n) uses, as mtl_addr_mapped makes it: not in the residency set
    pthread_mutex_lock(&g_mu); int i = pool_find((uint64_t)(uintptr_t)p, n); __weak id w = i >= 0 ? g_pools[i].buf : nil; int res = i >= 0 && g_pools[i].res; pthread_mutex_unlock(&g_mu);
    CHECK(w && !res, "the lookup made a chunk outside the residency set");
    return w;
}
int main(void) { @autoreleasepool {
    mtl_init(); const size_t MB = 1u << 20, na = 512 * MB;
    uint8_t *a = mmap(NULL, na, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (a == MAP_FAILED) return 1;
    mtl_pool_add(a, na);
    CHECK(mtl_addr_mapped((uint64_t)(uintptr_t)(a + 128), 4096), "a large pool's address is mapped");
    __weak id first = chunk_of(a + 128, 4096);
    CHECK(mtl_addr_mapped((uint64_t)(uintptr_t)(a + 256), 160 * MB), "a longer range of the same stride is mapped");   // lengthens the chunk: the short one is retired
    __weak id longer = chunk_of(a + 256, 160 * MB);
    CHECK(first && longer && first != longer, "the lengthened chunk's old buffer is alive while its release waits");
    CHECK(queued(first) == 1, "the old chunk is queued for deferred release");
    __weak id far = chunk_of(a + 400 * MB, 64);
    pthread_mutex_lock(&g_mu); big_full(&g_big[0]); pthread_mutex_unlock(&g_mu);   // consolidation retires every chunk
    CHECK(longer && far && queued(longer) == 1 && queued(far) == 1, "chunks consolidated away are alive and queued");
    mtl_sub_begin(); CHECK(mtl_wait(mtl_sub_commit(), 5000000000ull) && !mtl_last_commit_error(), "a submission passes the release fence");
    mtl_drain(); mtl_finish();
    CHECK(!queued(first) && !queued(longer) && !queued(far), "the releases ran");
    mtl_pool_del(a); mtl_finish(); munmap(a, na);
    printf("large pool chunk release: %d checks, %d failed\n", checks, failed); return failed != 0;
} }
