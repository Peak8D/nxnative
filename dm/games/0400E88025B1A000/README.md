# Star Fox (`0400E88025B1A000`)

A Switch 2 game: v131072, SDK 21.4.4 built for the Switch 2 (`Ounce`), NVN2. The Viper engine (`veclipse`), Wwise, libuv for its network layer. Modules `main`, `subsdk0`, `subsdk3` (the camera library) and `sdk`; `subsdk1` (C library) and `subsdk2` (DLSS) are not loaded. **Plays on the Mac**: title screen, menus, the campaign map and Corneria's first mission, with sound.

## What it needs

| Setting | Where | Why |
|---|---|---|
| `mem_reserve 0x1400000000`, `mem_alias 0xC00000000`, `mem_stack 0x10000000`, `app_pool 0x240000000` | [`game.tsv`](game.tsv) | the game maps its memory itself; the survey's 4.375 GB alias region ran out at the renderer's 3 GB block (`vrender_create`: Out of memory) |
| `net on` | `game.tsv` | real host sockets by default: see [Sockets](#sockets) |
| scene resolution scale 1.0 | [`patches.py`](patches.py) | the game renders its scene at half the output (768x432 under 1080p) and upscales it; the cut reads its resolution scale as 1.0 |

## Files

- A dump folder (`base/`, `updates/131072/`): the RomFS is `data.zip` (15.6 GB) and `veclipse_build.cvar`. `data.zip` is a VZIP archive: a zip file behind an index of the engine's own, 114,154 members named by hashes, each stored as LZ4 blocks of 64 KB that each take the 64 KB before them as their dictionary; the zip directory's CRC-32 is that of the decoded member.
- The game's configuration is one JSON member, `a513f6ee3dbafec6` (resolution scales, dynamic resolution, upscaler, per-platform values).

## Shaders

3,021 in [`shader_map.tsv`](shader_map.tsv), from three kinds of members of `data.zip`: the engine's shader objects (2,826: GLSLC's magic, but a code area and an NVN2 control section after an 8-byte hash), and two `VIPR` containers, one with records of control section then code (165 compute shaders, the webcam's face tracking network), one with a table of 28 control sections whose code areas are packed before it (the scene upscaler: without them the 3D picture was black).

2,936 translate. The 85 that do not are the face tracking network's (84 `IMMA`, one `0x3a9`): it runs only with a camera, and the host has none (open).

## Rendering

- The scene renders at the output's size times `render.resolution_scale_console` (0.5 on the Switch 2) times the dynamic resolution's floor (0.8), 768x432 for a 1080p output; the game's own upscaler doubles it and the present scales it into the 1080p crop of a 3840x2160 swap chain. The cut in `patches.py` makes the scale 1.0.
- DLSS runs at the scene's size (anti-aliasing): MetalFX's temporal scaler stands in. The game resizes the network after building it (`nvnDlssNetworkResize`).
- The texture streamer writes a 512x512x128 BC texture array (43 MB) and a 1024x1024x64 one (85 MB) between draws; each such write used to re-upload the whole array, up to 30 times a frame.

## Sockets

libuv makes its wakeup pair from two loopback TCP sockets (`main+0xbe32700`). Offline there are no sockets: the pair failed, a watcher went into the loop with no descriptor, the SDK's `poll` returned `EFAULT` and libuv aborted at boot. With real sockets (`net on`, or `DM_NET=1`) the pair binds. Loopback sockets offline, as the Switch's socket service gives them without a network, would boot it without the key (open).

## Driving it on the Mac

`DM_NET=0` forces the offline socket layer (the game then aborts at boot). `DM_NVN_DLSS_METALFX=0` replaces MetalFX with a bilinear copy, to tell a MetalFX problem from the game's.

## Open

- Loopback sockets offline, then `net` off.
- The face tracking network's 85 shaders (`IMMA`), for a camera the host does not have.
- 281 shaders take a texture whose handle the translator cannot trace (`Failed to find handle source for bindless access of type "texture2DArray"`); nothing wrong has been traced to them.

## Found on this game

- The VZIP archives and the Viper engine's shader objects and VIPR containers in the shader split (`dm/gfx/tools/shader_split.py`).
- NVN2 pixel shaders' input maps at the SPH's own offset (the vertex shaders of single-stage programs had been paired with no pixel shader inputs).
- Ampere `TXD` and `TLD4` through uniform-register handles, `SURED` reductions other than `ADD`, `PIXLD` (coverage mask and sample index), `BMSK`; a sampler handle alone is no texture candidate in the translator's phi handling.
- The hardware Opus batch decoder, Switch 2 audio output and input streams, GameChat's chatting user, the camera library with nothing connected, the socket service's event descriptors, Horizon's FreeBSD socket addresses.
- A texture written again while its upload still waits in the upload batch keeps that upload.
