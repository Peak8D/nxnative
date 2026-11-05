# The Legend of Zelda: Ocarina of Time (`0400486024DEC000`)

A Switch 2 game: update v131072, SDK 22.2.8 built for the Switch 2 (`Ounce`), NVN2 with Ampere shaders. Nintendo EPD's engine (sead / agl, Phive), modules `main`, `subsdk0` and `sdk`. In bring-up: the title, the file menu and the first gameplay (Link's house, Kokiri Forest) render lit on the Mac; foliage drawn by indirect task / mesh draws can hang the GPU (below).

## What it needs

| Setting | Where | Why |
|---|---|---|
| `mem_reserve 0x2e0000000` | [`game.tsv`](game.tsv) | the game asks for a 6.20 GB heap; the block also holds a 256 MB guard, the alias region and the stack |
| `mem_alias 0x118000000`, `mem_stack 0x10000000`, `app_pool 0x240000000` | `game.tsv` | the Switch 2's 9 GB pool |
| `sys_resource 0x3000000` | `game.tsv` | the NPDM declares 48 MB of system resource |
| `render_cond on` | `game.tsv` | agl wraps its clears in render conditions on timestamp records. Drawn unconditionally, the clears of the depth copy the lit pass tests `EQUAL` against (`main+0x246124`) wiped it, and gameplay showed only the sky, which tonemaps to white ([Conditional rendering evaluated at replay](../../../docs/reference/rendering.md#conditional-rendering-evaluated-at-replay)) |

## Shaders

42,854 in [`shader_map.tsv`](shader_map.tsv): `Shader/*.Nin_Ounce_NVN2.bfsha.zs`, `Lib/agl/*`, `Effect/*`, the game's `AglShader.sharcb` and `FoliageShader.sharcb`, terrain and layout packs; every NVN2 control section is covered. About 6,000 the first gameplay uses were not built yet under chain `59a6116d267c` (built on demand with `DM_NVN_DEMAND=1`, their draws skipped until then): a full `dmsh_build.py` run is needed before judging missing objects or ground textures.

## Rendering

- The scene renders at 1536x864 to 2048x1152 (dynamic resolution, R11G11B10F) into a 2560x1440 window texture the game crops (`nvnWindowSetCrop`); raw `DM_NVN_SHOT` shots show the crop in the top-left corner.
- Frame order in gameplay: a depth prepass, the sky over the whole target with no depth test, a copy of the prepass depth that the lit `Model` draws test `EQUAL` against (no depth writes), then transparent and effect passes, agl's NDSR upscaler and an ACES tonemap (`hdr_compose`, exposure 1.0 from its uniform block). Light intensities are physical (the sun about 73); the sky alone is about 29 before the tonemap.
- **Open:** the foliage's `DrawMeshTasksIndirect` draws (27 programs, all `FoliageShader` variants 0–2, at most 36 a frame) hang the GPU in about 3 runs of 5, intermittently, whatever the per-frame draw limit (`DM_NVN_MESH_IND_LIMIT`). Repeated hangs once panicked macOS. `DM_NVN_MESH_SKIP=indirect` leaves the grass out and plays safely.
- **Open:** NDSR's `d17eefaff4f0bc23` does image atomics on an R32Uint view of an RGBA8 texture, which Metal cannot run: they go to a write sink.
- Frame time in the first gameplay, M-series Mac, windows of 300 frames: a median of 40–56 ms with about 17 ms of GPU, the rest in submit (many draws still waiting for their shaders).
