# Yoshi and the Mysterious Book (`0400245021D2C000`)

A Switch 2 game: v0 (base), SDK 20.5.15 built for the Switch 2 (`Ounce`), NVN2. Unreal Engine 5, shader platform `SP_GLSL_SAGE`. Modules `main`, `subsdk2` (the SDK's movie library) and `sdk`; `subsdk0` and `subsdk1` (C library) are not loaded. **Plays on the Mac**: the opening, the title screen, the book and the first level, with sound.

## What it needs

| Setting | Where | Why |
|---|---|---|
| `mem_reserve 0x1028000000`, `mem_alias 0x1000000000`, `mem_stack 0x10000000`, `app_pool 0x240000000` | [`game.tsv`](game.tsv) | Unreal's `nninitStartup` reserves a 62 GB region and its allocator takes 37 GB of it: a 64 GB alias region; the Switch 2's 9 GB pool |
| `depth_mode zero` | `game.tsv` | the game sets `nvnDeviceSetDepthMode` with near = 0 |
| `ue_screen_percentage 100`, `dlss_native on` | `game.tsv` | the scene at the view's size: see [Resolution](#resolution) |

The rest of the profile is the survey's (`scripts/build_ipa_from_nsp.py`, `dm/games/profile_guess.py`).

## Building

From a dump folder (`base/exefs`, `base/romfs`):

```
scripts/build_ipa_from_nsp.py <dump folder> --platform mac-app --mac-sign none
```

The app is `<dump folder>/Yoshi.app`; run `Yoshi.app/Contents/MacOS/nxnative` directly. The work folder `<dump folder>/0400245021D2C000_build` keeps the extracted files, the shader package (`shader/pkg`) and the shader libraries (`shader/cache/b-<chain>`) for the next build. The translator needs .NET: `DOTNET_ROOT` naming its folder (Homebrew: `/opt/homebrew/opt/dotnet/libexec`).

## Shaders

133,154 in [`shader_map.tsv`](shader_map.tsv): the engine's global shader cache (`GlobalShaderCache-SP_GLSL_SAGE.bin`) and the material shader maps kept inline in the IoStore export data of `PJ045-Sage.utoc`, both Oodle-compressed and decoded by the game's own Oodle in the Mac host.

The full build of the libraries takes about 2.5 hours on an M4 Pro (one worker per logical CPU) and starts over at every change of the translation chain (`dm/gfx/nvn/dmsh_chain.h`). Its compile cache (`shader/cache/cas`) is shared with the on-demand builds below.

### On-demand builds

For work on the translator or the build, a run can build only the shaders it reaches. The host runs with `DM_NVN_DEMAND=1`: a shader with no library goes into `<library dir>/demand.txt` and its draws wait for it. `dm/gfx/tools/dmsh_demand.py` builds those keys into the same library as they come:

```
W=<dump folder>/0400245021D2C000_build
DOTNET_ROOT=/opt/homebrew/opt/dotnet/libexec GAME=0400245021D2C000 \
  python3 -u dm/gfx/tools/dmsh_demand.py $W/shader/pkg $W/shader/cache
```

Without `DOTNET_ROOT` every build fails: each key gets a `<key>.demand_failed` and the screen stays black. Delete those files after fixing the cause, or the host does not ask again. A scene's first visit builds a few hundred shaders (about a minute), so the first run through the opening is slow.

## Resolution

The game sets `r.ScreenPercentage` 50 and `r.SecondaryScreenPercentage.GameViewport` 80 at run time (its ini files say 100 for the second; dynamic resolution is off) and asks DLSS for its performance method: a 768x432 scene, made 1536x864 by DLSS and stretched to the 1920x1080 view. Its DLSS plugin asks for every method's optimal input at 1000x1000 and keeps the ratios. With `dlss_native` the host answers with the output's size, and `ue_screen_percentage` holds both percentages at 100: the scene is drawn at 1920x1080 and MetalFX's temporal scaler only anti-aliases. The GPU took about 8 ms a frame at 768x432 and takes about 19 ms at 1920x1080 on an M4 Pro (the first level).

## Rendering

- Instance culling runs in a compute shader whose predicated `LDG` loads zero their destination when the predicate is off. The translation read the address register after zeroing it when the address and destination shared a register, so every instance was culled and the 3D scenes were empty.
- Texture and sampler IDs come in two uniform-buffer words (NVN split handles): the texture ID in bits 0-19 of one, the sampler ID from bit 20 of the other. Each part is taken from its own word, and from the other word only when it is zero there (reflection `reg2` / `off2`). An OR of both words broke the title screen and the sky.
- Vertex fetches through uniform-buffer handles (bone matrices and the vertex factory's buffers) read texture buffers as 2D rows.
- The base pass of a skinned mesh tests its depth `GEQUAL` against the depth the velocity pass wrote. Metal's fast math rounded the two vertex shaders' skinned positions differently, which left holes in the characters, so vertex shaders compile without fast math (`VS_STRICT_FP`).
- Skinned meshes read their primitive ID offset from vertex attribute 16 (`UInt`, stream 6). The host kept only attributes 0-15, so the characters took another instance's transform: the Yoshis and the book were drawn elsewhere or off screen while their shadows showed. Attributes 16-31 are kept.
- Nanite's rasterizer writes 64-bit image atomics (`SURED.MAX` / `MIN.U64`) through images whose handles are in a uniform buffer, in handle banks other than 0.

## Driving it on the Mac

Holding Plus and Minus skips a cutscene (`DM_KEYS` `Plus+Minus~<seconds>`). `DM_NVN_DEPTHOVR` and `DM_NVN_CBDUMP_KEY` are read again at each frame, so `DM_CTL`'s `env` sets them in a running game, with `passdump 1` and `texdump <path>` for the frame they apply to.

## Open

- The walking flower on the book page did not draw before the attribute 16 fix; not checked since.
- After play, the save folder holds an empty `Slot00`; saving has not been looked into.

## Found on this game

- UE5 on the Switch 2 in the shader split: shader maps inline in IoStore export data and the global shader cache, Oodle entries walked by their `FShaderMapResourceCode` frame.
- The predicated `LDG` address, split texture handles in uniform buffers, vertex fetches through uniform-buffer handles, `TLD4` form `0x364`, `BRX`, image atomics in other banks and 64-bit texture atomics.
- Vertex shaders without fast math, vertex attributes 16-31, the uniform read extents in each reflection JSON for libraries built in parts.
- On-demand shader builds (`DM_NVN_DEMAND`, `dmsh_demand.py`) and one NSP script shader worker per logical CPU.
