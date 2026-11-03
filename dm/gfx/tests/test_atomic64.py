"""dmsh_atomic.merge_atomic64: the two 32-bit halves the translator writes for a 64-bit image MAX / MIN (InstEmitAmpereMem ASured) as one Metal 64-bit texture atomic.
The MSL is SPIRV-Cross's for Yoshi's Nanite rasterizers (FHWRasterizePS 0440c41c86980c94, FMicropolyRasterizeCS 0731e488063a0153), cut to the pair"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import dmsh_atomic

HEAD = '#include <metal_stdlib>\nusing namespace metal;\nstruct spvDescriptorSetBuffer0\n{\n    %s [[id(0)]];\n};\n'
PS = HEAD % 'texture2d<uint, access::read_write> fp_i_cb3_0_rg32ui' + '''fragment void main0(constant spvDescriptorSetBuffer0& spvDescriptorSet0 [[buffer(0)]], float4 gl_FragCoord [[position]])
{
    float _98 = gl_FragCoord.x;
    float _110 = gl_FragCoord.y;
    float _2112 = precise::clamp(gl_FragCoord.z, 0.0, 1.0);
    uint _232 = 7u;
    int2 _5192 = int2(int(_98), int(_110));
    if (all(uint2(_5192) < uint2(spvDescriptorSet0.fp_i_cb3_0_rg32ui.get_width(), spvDescriptorSet0.fp_i_cb3_0_rg32ui.get_height())))
    {
        uint _5205 = spvDescriptorSet0.fp_i_cb3_0_rg32ui.atomic_fetch_max(uint2(_5192), as_type<uint>(_2112)).x;
    }
    int _2114 = int(0u);
    int2 _5212 = int2(int(_98), int(_110));
    if (all(uint2(_5212) < uint2(spvDescriptorSet0.fp_i_cb3_0_rg32ui.get_width(), spvDescriptorSet0.fp_i_cb3_0_rg32ui.get_height())))
    {
        uint _5222 = spvDescriptorSet0.fp_i_cb3_0_rg32ui.atomic_fetch_max(uint2(_5212), as_type<uint>(_232)).x;
    }
    int _2116 = int(0u);
}
'''
ARRAY = HEAD % 'texture2d_array<uint, access::read_write> cp_i_cb3_0_rg32ui' + '''kernel void main0(constant spvDescriptorSetBuffer0& spvDescriptorSet0 [[buffer(0)]], uint3 gl_GlobalInvocationID [[thread_position_in_grid]])
{
    uint _6655 = gl_GlobalInvocationID.y;
    float _7133 = 0.5;
    if (gl_GlobalInvocationID.x > 3u)
    {
        int3 _16673 = int3(int(gl_GlobalInvocationID.x), int(_6655), 0);
        if (all(uint3(_16673) < uint3(spvDescriptorSet0.cp_i_cb3_0_rg32ui.get_width(), spvDescriptorSet0.cp_i_cb3_0_rg32ui.get_height(), spvDescriptorSet0.cp_i_cb3_0_rg32ui.get_array_size())))
        {
            uint _16685 = spvDescriptorSet0.cp_i_cb3_0_rg32ui.atomic_fetch_min(uint3(_16673).xy, uint3(_16673).z, as_type<uint>(_7133)).x;
        }
        int _7135 = int(0u);
        int3 _16692 = int3(int(gl_GlobalInvocationID.x), int(_6655), 0);
        if (all(uint3(_16692) < uint3(spvDescriptorSet0.cp_i_cb3_0_rg32ui.get_width(), spvDescriptorSet0.cp_i_cb3_0_rg32ui.get_height(), spvDescriptorSet0.cp_i_cb3_0_rg32ui.get_array_size())))
        {
            uint _16702 = spvDescriptorSet0.cp_i_cb3_0_rg32ui.atomic_fetch_min(uint3(_16692).xy, uint3(_16692).z, uint(_6655)).x;
        }
        int _7137 = int(0u);
    }
}
'''


def compiles(src):
    if not shutil.which('xcrun'): return None
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 't.metal')
        with open(p, 'w') as f: f.write(src)
        r = subprocess.run(['xcrun', '-sdk', 'macosx', 'metal', '-c', '-O1', p, '-o', os.path.join(d, 't.air')], capture_output=True, text=True)
        return r.returncode == 0 or r.stderr


class Merge(unittest.TestCase):
    def test_pixel_pair_becomes_one_64bit_max(self):
        out = dmsh_atomic.merge_atomic64(PS)
        self.assertIn('texture2d<ulong, access::read_write> fp_i_cb3_0_rg32ui', out)
        self.assertEqual(re.findall(r'\.atomic_\w+\(.*\);', out), ['.atomic_max(uint2(_5192), (ulong(as_type<uint>(_2112)) << 32) | ulong(as_type<uint>(_232)));'])
        self.assertNotIn('atomic_fetch_', out)
        self.assertIn(compiles(out), (True, None))

    def test_array_pair_becomes_one_64bit_min(self):
        out = dmsh_atomic.merge_atomic64(ARRAY)
        self.assertIn('texture2d_array<ulong, access::read_write> cp_i_cb3_0_rg32ui', out)
        self.assertEqual(re.findall(r'\.atomic_\w+\(.*\);', out), ['.atomic_min(uint3(_16673).xy, uint3(_16673).z, (ulong(as_type<uint>(_7133)) << 32) | ulong(uint(_6655)));'])
        self.assertIn(compiles(out), (True, None))

    def test_a_lone_half_fails_the_shader(self):
        lone = re.sub(r'    int2 _5212 = .*?\n    \}\n', '', PS, flags=re.S)
        self.assertEqual(lone.count('atomic_fetch_max'), 1)
        with self.assertRaises(ValueError): dmsh_atomic.check_msl(lone)

    def test_halves_of_different_texels_are_not_merged(self):
        other = PS.replace('int2 _5212 = int2(int(_98), int(_110));', 'int2 _5212 = int2(int(_110), int(_98));')
        with self.assertRaises(ValueError): dmsh_atomic.check_msl(other)

    def test_rg32_image_without_atomics_keeps_uint(self):
        src = HEAD % 'texture2d<uint, access::read_write> fp_i_cb2_48_rg32ui' + 'fragment void main0() {}\n'
        self.assertEqual(dmsh_atomic.merge_atomic64(src), src)

    def test_32bit_atomics_untouched(self):
        src = PS.replace('rg32ui', 'r32ui')
        self.assertEqual(dmsh_atomic.merge_atomic64(src), src)


if __name__ == '__main__':
    unittest.main()
