// Unreal console variables found by their registered names: the texture streaming pool the profile's ue_texture_pool raises, the screen percentages its ue_screen_percentage holds.
// The rewriter finds each TAutoConsoleVariable the host may write in main (dm/tools/ue_cvar.py: its static constructor's registration, in any Unreal 4 / 5 build) and names it in main's
// fixups (UE_CVAR:<name>); the object holds at +0x10 the pointer to the variable's value, the game thread's copy, then the render thread's. Called on every present (hle_nvn.c)
#include "hle.h"
#include <mach/mach.h>
#include <TargetConditionals.h>

static int rd(uint64_t a, void *out, size_t n) { vm_size_t got = 0; return a >= 0x1000 && vm_read_overwrite(mach_task_self(), (vm_address_t)a, n, (vm_address_t)out, &got) == KERN_SUCCESS && got == n; }

// main's offset of the TAutoConsoleVariable registered under `name`, 0 if the rewriter found none (not an Unreal main, not one of ue_cvar.py's NAMES, or registered at two objects)
uint64_t dm_ue_cvar(const char *name) {
    for (int i = 0; i < g_nmod; i++) if (!strcmp(g_mod[i].name, "main"))
        for (int k = 0; k < g_mod[i].n_ue_cvar; k++) if (!strcmp(g_mod[i].ue_cvar[k].name, name)) return g_mod[i].ue_cvar[k].off;
    return 0;
}

// ue_texture_pool: r.Streaming.PoolSize, the megabytes Unreal's texture streamer keeps resident; past it the streamer drops the top mips of the textures in view. The game's
// device profile sets it (DRAGON QUEST VII Reimagined: 200 on the Switch, 1000 on Windows) and the streaming manager reads it again whenever it changes. The host raises a smaller
// value the game sets to the target, whenever the game sets it back; a larger one, or -1 (no pool size: the engine sizes its pool otherwise), stays. The profile key applies on the
// Mac; on iOS only DM_UE_TEXTURE_POOL turns it on (Documents/env.txt), until an iPhone run shows the memory fits. DM_UE_TEXTURE_POOL=0 leaves the game's value
static void pool_tick(void) {
    static int state = -1, target, nlog; static uint64_t obj_off; static const char *src;
    if (!state) return;
    if (state < 0) {
        state = 0;
        const char *e = getenv("DM_UE_TEXTURE_POOL");
        target = e ? atoi(e) : TARGET_OS_IPHONE ? 0 : DM_GAME_UE_TEXTURE_POOL; src = e ? "DM_UE_TEXTURE_POOL" : "game profile ue_texture_pool";
        if (target <= 0) { if (DM_GAME_UE_TEXTURE_POOL) LOG("main: Unreal r.Streaming.PoolSize left as the game sets it (%s)", e ? src : "iOS without DM_UE_TEXTURE_POOL"); return; }
        obj_off = dm_ue_cvar("r.Streaming.PoolSize");
        if (!obj_off) { LOG("[RED] main: Unreal r.Streaming.PoolSize not found in main by the rewriter (ue_cvar.py; %s %d MB not applied)", src, target); return; }
        LOG("main: Unreal r.Streaming.PoolSize at main+%#llx, raised to %d MB (%s)", (unsigned long long)obj_off, target, src);
        state = 1;
    }
    uint8_t *base = NULL;
    for (int i = 0; i < g_nmod; i++) if (!strcmp(g_mod[i].name, "main")) base = g_mod[i].base;
    uint64_t val;
    if (!base || !rd((uint64_t)(uintptr_t)(base + obj_off + 0x10), &val, sizeof val) || !val) return;   // not created yet
    for (int c = 0; c < 2; c++) {
        int32_t cur, *p = (int32_t *)(uintptr_t)(val + 4 * (uint64_t)c);
        if (!rd((uint64_t)(uintptr_t)p, &cur, sizeof cur) || cur <= 0 || cur >= target) continue;
        dm_guest_write_prepare(p, sizeof *p); __atomic_store_n(p, target, __ATOMIC_RELAXED);
        if (nlog < 8) { nlog++; LOG("main: Unreal r.Streaming.PoolSize (%s thread) %d → %d MB (%s)", c ? "render" : "game", cur, target, src); }
    }
}

// ue_screen_percentage <scene> [<output>]: r.ScreenPercentage (the scene's size in the upscaler's input) and r.SecondaryScreenPercentage.GameViewport (the upscaler's output in the game's
// view) held at those percentages (one value: both). Yoshi and the Mysterious Book sets 50 and 80 at run time (its ini files say 100 for the second), so DLSS made 1536x864 of a 768x432 scene for a 1920x1080 view; with 100
// and the profile's dlss_native the scene is drawn at 1920x1080 and MetalFX only anti-aliases. Written whenever the game sets a value back; DM_UE_SCREEN_PERCENTAGE overrides (0: the game's)
static void pct_tick(void) {
    static int state = -1, nlog; static float tg[2]; static uint64_t off[2]; static const char *src;
    static const char *const name[2] = {"r.ScreenPercentage", "r.SecondaryScreenPercentage.GameViewport"};
    if (!state) return;
    if (state < 0) {
        state = 0;
        const char *e = getenv("DM_UE_SCREEN_PERCENTAGE");
        const char *v = e ? e : DM_GAME_UE_SCREEN_PERCENTAGE; char *end; src = e ? "DM_UE_SCREEN_PERCENTAGE" : "game profile ue_screen_percentage";
        tg[0] = strtof(v, &end); while (*end == ' ' || *end == ',') end++; tg[1] = *end ? strtof(end, NULL) : tg[0];
        if (!(tg[0] > 0) || !(tg[1] > 0)) return;
        for (int k = 0; k < 2; k++) if (!(off[k] = dm_ue_cvar(name[k]))) { LOG("[RED] main: Unreal %s not found in main by the rewriter (ue_cvar.py; %s %s not applied)", name[k], src, v); return; }
        LOG("main: Unreal r.ScreenPercentage held at %g, r.SecondaryScreenPercentage.GameViewport at %g (%s)", tg[0], tg[1], src);
        state = 1;
    }
    uint8_t *base = NULL;
    for (int i = 0; i < g_nmod; i++) if (!strcmp(g_mod[i].name, "main")) base = g_mod[i].base;
    for (int k = 0; k < 2 && base; k++) {
        uint64_t val;
        if (!rd((uint64_t)(uintptr_t)(base + off[k] + 0x10), &val, sizeof val) || !val) continue;   // not created yet
        for (int c = 0; c < 2; c++) {
            float cur, *p = (float *)(uintptr_t)(val + 4 * (uint64_t)c);
            float target = tg[k];
            if (!rd((uint64_t)(uintptr_t)p, &cur, sizeof cur) || cur == target) continue;
            uint32_t bits; memcpy(&bits, &target, sizeof bits); dm_guest_write_prepare(p, sizeof *p); __atomic_store_n((uint32_t *)p, bits, __ATOMIC_RELAXED);
            if (nlog < 8) { nlog++; LOG("main: Unreal %s (%s thread) %g → %g (%s)", name[k], c ? "render" : "game", cur, target, src); }
        }
    }
}

void dm_ue_cvar_tick(void) { pool_tick(); pct_tick(); }
