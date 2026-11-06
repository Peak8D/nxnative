# The Legend of Zelda: Ocarina of Time (`0400486024DEC000`)

A Switch 2 game: update v131072, SDK 22.2.8 built for the Switch 2 (`Ounce`), NVN2 with Ampere shaders. Nintendo EPD's engine (sead / agl, Phive), modules `main`, `subsdk0` and `sdk`. On this branch (`oot-full-build`) the game plays on a Mac with Apple silicon: the title, the movies, the file menu and gameplay in Kokiri Forest render lit, with foliage, fog, water and terrain, at about 60 fps on an M5 Pro.

## Playing it

### What you need

- **Your own dump of the game:** The Legend of Zelda: Ocarina of Time (Switch 2, title ID `0400486024DEC000`), base game plus update v131072, as one `.nup` file or the same files unpacked into a folder (`base/exefs`, `base/romfs`, `updates/131072/...`; [Switch 2 games](../../../README.md#switch-2-games)). **No keys:** Switch 2 games are built from the NUP container or its files, and neither needs `prod.keys`. Nothing from the game (code, textures, shaders, saves) is in this repository.
- **A Mac with Apple silicon** and the project's toolchain: macOS 26.6 or later, Xcode 27.2 (its Metal compiler builds the shaders), Homebrew and an arm64 Python 3.12 or newer ([README](../../../README.md)). The prebuilt shader translator needs the .NET 10 runtime (`brew install --cask dotnet-sdk`). Tested on an M5 Pro with 24 GB.
- **Memory:** the game's footprint is about 10.4-10.8 GB in gameplay (peaks ~11.1 GB), up to ~10.5 GB of it GPU allocations, 6-7 GB wired at gameplay entry. **A 24 GB Mac is tight:** close other large apps and builds before playing; the margin above memory-pressure level 2 was 0.3-0.7 GB and depended on what else ran. 32 GB or more is comfortable. The iPhone and iPad are not covered.
- **Disk:** the unpacked RomFS image is ~24 GB; the shader library and its cache come on top.

### Building

The shader chain of this branch is generated with two build options, `DMSH_RAW_LOAD_QUADS=1` and `DMSH_RAW_FAST_LOOKUP=1` ([Adjacent raw global memory reads](../../../docs/reference/shaders.md#adjacent-raw-global-memory-reads), [Raw global memory pool lookup through a directory](../../../docs/reference/shaders.md#raw-global-memory-pool-lookup-through-a-directory)). `dm/gfx/nvn/dmsh_chain.h` already names that chain (`1b3fb25407ac`) and the host is compiled with it: **set both options for every shader build**, or the libraries land under another chain folder and the host loads none of them.

```sh
export DMSH_RAW_LOAD_QUADS=1 DMSH_RAW_FAST_LOOKUP=1
python3 dm/gfx/tools/dmsh_chain.py      # must print 1b3fb25407ac, the chain in dm/gfx/nvn/dmsh_chain.h
python3 scripts/build_ipa_from_nsp.py <your .nup or folder> --platform mac-app \
    --env-file dm/games/0400486024DEC000/recommended_env.txt
```

The script unpacks the NUP (about a minute for the 25.6 GB container), rewrites the code modules, splits and builds the shaders (~42,850; about 40-45 minutes on an M5 Pro), builds the host and packs a Mac app. `--platform mac` instead builds for running from the work folder and prints the run command.

By hand ([Build by hand (Mac)](../../../docs/development.md#build-by-hand-mac)), with `X` the folder holding the extracted v131072 `ExeFS/` and `RomFS.img` (`<id>_build/extract/v131072` after the script's unpack step) and `W` a work folder:

```sh
G=0400486024DEC000; X=<extracted v131072>; W=<work folder>
export DMSH_RAW_LOAD_QUADS=1 DMSH_RAW_FAST_LOOKUP=1
# 1. rewrite the code modules
for m in $(python3 dm/games/game.py $G modules); do python3 dm/tools/patch_nso.py $X/ExeFS/$m $W/build/patched_v131072 $m; done
# 2. shaders: split, then build into <cache>/b-1b3fb25407ac/
DM_GAME=$G python3 dm/gfx/tools/shader_split.py $X/RomFS.img $X/ExeFS/main $W/shader/pkg dm/games/$G/shader_map.tsv dm/games/$G/shader_facts.tsv dm/games/$G/shader_pairs.tsv
DM_GAME=$G python3 dm/gfx/tools/dmsh_build.py $W/shader/pkg $W/build/cache/metal_shader_cache
# 3. the host
make -C dm/host GAME=$G dmhost
```

Run it as in step 5 of [Build by hand (Mac)](../../../docs/development.md#build-by-hand-mac), with the switches of `recommended_env.txt` in the environment.

### Settings

The profile has no key for host switches, so the tested ones are in [`recommended_env.txt`](recommended_env.txt): pass it to the build with `--env-file` (as above), or copy it to `~/Library/Application Support/nxnative/0400486024DEC000/env.txt`.

| Switch | Value | What it does |
|---|---|---|
| `DM_TEX_RELEASE` | `2` | Gives back the guest copy of a texture's storage once the GPU has it (`CPU_NO_ACCESS` pools, and still-protected pages of CPU-visible uploads): ~2.3 GB less footprint |
| `DM_TEX_LAZY` | `2` | A pool walk makes no host texture for IDs in `CPU_NO_ACCESS` pools (render targets are made when attached or copied). Not `1`: textures only sampled through the bindless pool then read the dummy (black rocks, flat fog and water) |
| `DM_GMEM_KEEP` | `new` | When the raw global memory pool table overflows, its partial tier keeps the newest pools (the current area's foliage data) |
| `DM_NVN_MESH_SKIP` | `none` | Draws every task / mesh draw (the foliage); safe with the guards below |
| `DM_PSO_PREBUILD` | `0` | No pipeline prebuild at start |

On by default in the code (`0` turns each off): `DM_TEX_REL_DONOR` (a texture registered again over released storage is a GPU copy of the one released there), `DM_NVN_GPUW_RANGE` (GPU writes tracked per storage range, so the terrain heightmaps keep their heights), `DM_NVN_MESH_GUARD` (indirect task / mesh draws skipped while the pool table leaves pools out, and after a command buffer error), `DM_GMEM_PTRSCAN`, `DM_POOL_INCR`, `DM_ALIAS_NOTE`, `DM_ALIAS_BATCH`, `DM_POOL_RECYCLE`, `DM_DEPTH_FAMILY`, `DM_POOL_SKIP_STALE`, `DM_NVN_FB_DSNAP` (with `DM_NVN_FB_DPOOL=2`), `DM_NVN_IXC_PAST`. `DM_NDSR` and `DM_NDVE` default to `metalfx` (`game` runs the game's own shaders). Optional: `DM_LANG=<code>` for the game language, `DM_METAL_HUD=1` for Apple's performance HUD.

### Known issues

- **First-use shader pauses:** a pipeline first met in an area pauses 80 ms to 2.3 s while Metal compiles it; the pipeline archive learns it, and the next run does not pause there.
- **Area-entry hitches:** gameplay entry and new areas upload ~1 GB of textures in a burst; ~10 frames of 40-120 ms follow.
- **Movies:** with `DM_NDVE=metalfx` the game's video enhancement network is skipped and MetalFX upscales instead, so movies are slightly sharper with faint edge ringing and less denoising than on the console. `DM_NDVE=game` runs the network at ~26 fps.
- **No Dynamic FPS:** the game's time step is fixed per frame, so when a frame takes longer than 16.7 ms the game runs slower than real time. A community "Dynamic FPS" patch that scales the time step by the real frame time exists, but it is under another license and is **not included** here.
- **Memory on 24 GB Macs:** see above; runs were stopped by a memory watchdog when the Mac had less than ~14 GB available before starting.
- **Not checked:** Lost Woods, Hyrule Field and later areas; the iPhone and iPad.

### Performance (M5 Pro, 24 GB, Kokiri Forest, 260 s walking runs)

| | Frame time p50 / p95 | GPU p50 / p95 |
|---|---|---|
| Kokiri Forest gameplay | 16.8-16.9 / 22.2 ms (~60 fps) | 15.5-16.2 / 17.3-18.0 ms (GPU span 12-16 ms in profiled frames) |
| Frames over 20 / 25 / 33 / 50 ms, of ~4,500 | 111 / 5 / 2 / 1 (p99 21.3 ms) | |
| Title movies | 16.66 ms (60 fps) | 3.5-4.1 ms |

## What it needs

| Setting | Where | Why |
|---|---|---|
| `mem_reserve 0x2e0000000` | [`game.tsv`](game.tsv) | the game asks for a 6.20 GB heap; the block also holds a 256 MB guard, the alias region and the stack |
| `mem_alias 0x118000000`, `mem_stack 0x10000000`, `app_pool 0x240000000` | `game.tsv` | the Switch 2's 9 GB pool |
| `sys_resource 0x3000000` | `game.tsv` | the NPDM declares 48 MB of system resource |
| `render_cond on` | `game.tsv` | agl wraps its clears in render conditions on timestamp records. Drawn unconditionally, the clears of the depth copy the lit pass tests `EQUAL` against (`main+0x246124`) wiped it, and gameplay showed only the sky, which tonemaps to white ([Conditional rendering evaluated at replay](../../../docs/reference/rendering.md#conditional-rendering-evaluated-at-replay)) |
| a raw pool table of 8,192 entries | `dmsh_abi.h`, the translator | gameplay reaches ~4,100-4,500 memory pools; with 4,096 the foliage task shaders read the ranges of left-out pools as 0 and hung the GPU |

## Shaders

42,851 in [`shader_map.tsv`](shader_map.tsv): `Shader/*.Nin_Ounce_NVN2.bfsha.zs`, `Lib/agl/*`, `Effect/*`, the game's `AglShader.sharcb` and `FoliageShader.sharcb`, terrain and layout packs; every NVN2 control section is covered. A full build with this branch's translator gave 42,850 libraries, 2 of them failing their Metal compile.

## Rendering

- The scene renders at 1536x864 to 2048x1152 (dynamic resolution, R11G11B10F) into a 2560x1440 window texture the game crops (`nvnWindowSetCrop`); raw `DM_NVN_SHOT` shots show the crop in the top-left corner.
- Frame order in gameplay: a depth prepass, the sky over the whole target with no depth test, a copy of the prepass depth that the lit `Model` draws test `EQUAL` against (no depth writes), then transparent and effect passes, agl's NDSR upscaler (replaced by MetalFX's spatial scaler, `DM_NDSR`) and an ACES tonemap (`hdr_compose`, exposure 1.0 from its uniform block). Light intensities are physical (the sun about 73); the sky alone is about 29 before the tonemap.
- The foliage is drawn by `DrawMeshTasksIndirect` (27 programs, all `FoliageShader` variants 0-2, at most 36 a frame). Its GPU hangs came from the raw pool table overflowing (pools left out read as an empty range, and the task shaders looped 2^27 times): fixed by the 8,192-entry table, with `DM_NVN_MESH_GUARD` as a safety net.
- NDSR's `d17eefaff4f0bc23` does image atomics on an R32Uint view of an RGBA8 texture, which Metal cannot run; with `DM_NDSR=metalfx` that chain does not run.

## Credits

- This branch builds on penis6's [nxnative-penis6](https://git.eden-emu.dev/penis6/nxnative-penis6) `oot` branch (the Ocarina of Time profile, its services and diagnostics; its commits are kept as history), used with the author's permission.
- [seedds/nxnative](https://github.com/seedds/nxnative), the project it forks (MIT), and its contributors.
- Translator changes from seedds/nxnative pull requests: raw-load quads, [#1405](https://github.com/seedds/nxnative/pull/1405) by nanotchec; live shader inputs kept by dead-code removal, [#1343](https://github.com/seedds/nxnative/pull/1343) by nanotchec; float add atomics, [#1359](https://github.com/seedds/nxnative/pull/1359) by lunks. [#1360](https://github.com/seedds/nxnative/pull/1360) by lunks (a pool table that grows with the pools) is the general form of the 8,192-entry table used here.
- fpedrolucas95's NDVE / RLA1 / NDSR item readers (merged in the base branch).
