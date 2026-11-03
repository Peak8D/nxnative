# Star Fox (0400E88025B1A000): the rewriter's patches of this game's own builds. dm/tools/patch_nso.py loads every profile's patches.py and applies those of a module's NSO build ID
CUTS = {
  # 1.0.x (v131072) main: the renderer (vrender.impl.c, main+0xb447300) takes its scene resolution scale from render.resolution_scale_console / _handheld (0.5 on the
  # Switch 2: 768x432 under the 1080p output). Read as 1.0 instead (ldr s0, [sp, #0x3a0] after the cvar lookup at main+0xb4498a8 -> fmov s0, #1.0)
  'a7730d0f74d168d90422354c8317f76f': [
    (0xb4498ac, 'e0a343bd', '00102e1e', 'scene resolution scale 1.0 (render.resolution_scale_console / _handheld read as 1.0)'),
  ],
}

# How general each patch is and could be (dm/tools/docs_patches.py "Binding", listed in docs/agents/game-patches.md)
BINDING = [
    {'what': 'Viper renderer: scene resolution scale 1.0', 'now': 'build', 'goal': 'game', 'purpose': 'rendering',
     'checked': 'v131072 main: the scene visibly sharper on the Mac (the render targets\' new size not measured)',
     'why': 'the scale is a cvar the renderer looks up by name and hash at its creation (main+0xb447300); the read is one instruction of one build. A function-matched cut, or a '
            'host hook of the engine\'s cvar lookup, would carry it to other builds (open)'},
]
