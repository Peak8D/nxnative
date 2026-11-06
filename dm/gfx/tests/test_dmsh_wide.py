"""Storage words the translator split, read whole again (dmsh_wide.py wide_loads): which words become one read, which are left alone, and the compile."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import dmsh_chain  # noqa: E402
from dmsh_wide import raw_fast_lookup, raw_load_quads, wide_loads   # noqa: E402
import dmsh_abi   # noqa: E402

# Splatoon Raiders' Hoian_UBER (3e5a302ac52b73bf) as spirv-cross writes it, cut down: one light's words from fp_s3 at 80 * index + 16 .. 44, a written buffer,
# and words in two blocks
LIGHT = '''#include <metal_stdlib>
#include <simd/simd.h>

using namespace metal;

struct _29
{
    uint _m0[1];
};

struct spvDescriptorSetBuffer0
{
    const device _29* fp_s2 [[id(0)]];
    const device _29* fp_s3 [[id(1)]];
    device _29* fp_s4 [[id(2)]];
};

struct main0_out
{
    float4 m_0 [[color(0)]];
};

struct main0_in
{
    float4 m_1 [[user(locn0)]];
};

fragment main0_out main0(main0_in in [[stage_in]], constant spvDescriptorSetBuffer0& spvDescriptorSet0 [[buffer(0)]])
{
    main0_out out = {};
    int _3483 = int(in.m_1.x) * 80;
    int _3485 = _3483 + 16;
    int _3487 = _3483 + 32;
    uint _3493 = uint(int(uint(_3485) >> uint(2)));
    float _3495 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3493)]);
    uint _3499 = uint(int(uint(_3487) >> uint(2)));
    float _3501 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3499)]);
    int _3503 = _3487 + 4;
    uint _3505 = uint(int(uint(_3503) >> uint(2)));
    float _3507 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3505)]);
    int _3509 = _3487 + 8;
    uint _3511 = uint(int(uint(_3509) >> uint(2)));
    float _3513 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3511)]);
    int _3515 = _3487 + 12;
    uint _3517 = uint(int(uint(_3515) >> uint(2)));
    uint _3519 = (*spvDescriptorSet0.fp_s3)._m0[int(_3517)];
    uint _20 = uint(int(uint(_3483) >> uint(2)));
    uint _21 = (*spvDescriptorSet0.fp_s4)._m0[int(_20)];
    int _22 = _3483 + 4;
    uint _23 = uint(int(uint(_22) >> uint(2)));
    uint _24 = (*spvDescriptorSet0.fp_s4)._m0[int(_23)];
    int _30 = _3483 + 64;
    int _31 = _3483 + 68;
    uint _32 = uint(int(uint(_30) >> uint(2)));
    uint _33 = uint(int(uint(_31) >> uint(2)));
    uint _34 = (*spvDescriptorSet0.fp_s2)._m0[int(_33)];
    uint _35 = (*spvDescriptorSet0.fp_s2)._m0[int(_32)];
    int _40 = _3483 + 48;
    uint _41 = uint(int(uint(_40) >> uint(2)));
    uint _42 = (*spvDescriptorSet0.fp_s2)._m0[int(_41)];
    if (_3495 > 0.0)
    {
        int _43 = _40 + 4;
        uint _44 = uint(int(uint(_43) >> uint(2)));
        _42 += (*spvDescriptorSet0.fp_s2)._m0[int(_44)];
    }
    out.m_0 = float4(_3495 + _3501, _3507 + _3513, as_type<float>(_3519 + _21 + _24), as_type<float>(_34 + _35 + _42));
    return out;
}
'''


class WideLoads(unittest.TestCase):
    def test_words_of_one_light(self):
        out = wide_loads(LIGHT)
        # bytes 32..44 from one value: one packed_uint4 at the first word, each word its lane (float or uint alike)
        self.assertIn('packed_uint4 _dmw0 = *(const device packed_uint4*)&(*spvDescriptorSet0.fp_s3)._m0[int(_3499)];', out)
        for lane, var in zip('xyz', ('_3501', '_3507', '_3513')): self.assertIn(f'float {var} = as_type<float>(_dmw0.{lane});', out)
        self.assertIn('uint _3519 = _dmw0.w;', out)
        # byte 16 is alone on its 16-byte step: as it was
        self.assertIn('float _3495 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3493)]);', out)
        self.assertEqual(out.count('_dmw'), 5)   # one declaration, four lanes

    def test_left_alone(self):
        out = wide_loads(LIGHT)
        self.assertIn('(*spvDescriptorSet0.fp_s4)._m0[int(_20)]', out)   # a buffer the shader may write
        self.assertIn('(*spvDescriptorSet0.fp_s4)._m0[int(_23)]', out)
        self.assertIn('(*spvDescriptorSet0.fp_s2)._m0[int(_33)]', out)   # the higher word read first
        self.assertIn('(*spvDescriptorSet0.fp_s2)._m0[int(_32)]', out)
        self.assertIn('(*spvDescriptorSet0.fp_s2)._m0[int(_41)]', out)   # its neighbour is in another block
        self.assertIn('(*spvDescriptorSet0.fp_s2)._m0[int(_44)]', out)

    def test_writes_end_a_run(self):
        # a store through another binding (which may be the same memory), an atomic, a barrier or a new common value between the words: each word read where it was
        first = '    float _3501 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3499)]);\n'
        for between in ('    (*spvDescriptorSet0.fp_s4)._m0[int(_20)] = 1u;', '    (*spvDescriptorSet0.fp_s4)._m0[int(_20)] += 1u;',
                        '    uint _90 = atomic_fetch_add_explicit((device atomic_uint*)&(*spvDescriptorSet0.fp_s4)._m0[int(_20)], 1u, memory_order_relaxed);',
                        '    threadgroup_barrier(mem_flags::mem_device);', '    _3487 = _3483 + 48;'):
            out = wide_loads(LIGHT.replace(first, first + between + '\n'))
            self.assertNotIn('_dmw', out, between)
            self.assertIn('(*spvDescriptorSet0.fp_s3)._m0[int(_3505)]', out)

    def test_nothing_to_do(self):
        plain = LIGHT.replace('const device', 'device')
        self.assertEqual(wide_loads(plain), plain)

    @unittest.skipUnless(shutil.which('xcrun') and sys.platform == 'darwin', 'Metal compiler')
    def test_compiles(self):
        with tempfile.TemporaryDirectory() as d:
            for name, src in (('before', LIGHT), ('after', wide_loads(LIGHT))):
                m = os.path.join(d, name + '.metal'); Path(m).write_text(src)
                r = subprocess.run(['xcrun', '-sdk', 'macosx', 'metal', '-c', '-O1', '-fpreserve-invariance', m, '-o', os.path.join(d, name + '.air')],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


def raw_source(entries):
    """Synthetic fixed-table helper and four words of a raw 128-bit load."""
    prefix = '''
struct _10 { uint _m0[1]; };
struct spvDescriptorSetBuffer0
{
    device _10* cp_s5 [[id(7)]];
};
static inline __attribute__((always_inline))
int _900(thread const int& _1, thread const int& _2, device _10& cp_s5)
{
'''
    next_id = 100
    previous = '0'
    for bit in reversed(range(entries.bit_length() - 1)):
        probe, row, li, low, hi, high, less, equal, lo_le, tied, take, index = (
            f'_{i}' for i in range(next_id, next_id + 12))
        prefix += f'''    int {probe} = {previous} + {1 << bit};
    int {row} = {probe} * 8;
    int {li} = {row} + 0;
    float {low} = as_type<float>(cp_s5._m0[{li}]);
    int {hi} = {row} + 1;
    float {high} = as_type<float>(cp_s5._m0[{hi}]);
    bool {less} = as_type<uint>({high}) < uint(_2);
    bool {equal} = as_type<int>({high}) == _2;
    bool {lo_le} = as_type<uint>({low}) <= uint(_1);
    bool {tied} = {equal} && {lo_le};
    bool {take} = {less} || {tied};
    int {index} = {take} ? {probe} : {previous};
'''
        previous = index
        next_id += 12
    prefix += f'''    int _310 = {previous} * 8;
    int _311 = _310 + 2;
    float _312 = as_type<float>(cp_s5._m0[_311]);
    int _313 = _310 + 3;
    float _314 = as_type<float>(cp_s5._m0[_313]);
    bool _315 = uint(_2) < as_type<uint>(_314);
    bool _316 = _2 == as_type<int>(_314);
    bool _317 = uint(_1) < as_type<uint>(_312);
    bool _318 = _316 && _317;
    bool _319 = _315 || _318;
    if (_319)
    {{
        int _320 = _310 + 4;
        float _321 = as_type<float>(cp_s5._m0[_320]);
        int _322 = _1 + as_type<int>(_321);
        bool _323 = uint(_322) < uint(_1);
        int _324 = int(_323);
        int _325 = _310 + 5;
        float _326 = as_type<float>(cp_s5._m0[_325]);
        int _327 = _2 + as_type<int>(_326);
        int _328 = _327 + _324;
        uint _329 = *(reinterpret_cast<device uint*>(as_type<ulong>(uint2(uint(_322), uint(_328)))));
        float _330 = as_type<float>(_329);
        return as_type<int>(_330);
    }}
    return 0;
}}
kernel void main0(constant spvDescriptorSetBuffer0& spvDescriptorSet0 [[buffer(0)]], uint id [[thread_position_in_grid]])
{{
    int _500 = int(id);
    int _501 = 4;
'''
    for i, offset in enumerate((4, 8, 12)):
        lo, wraps, carry, hi = (500 + 4 * (i + 1) + j for j in range(4))
        prefix += f'''    int _{lo} = _500 + {offset};
    bool _{wraps} = uint(_{lo}) < uint(_500);
    int _{carry} = int(_{wraps});
    int _{hi} = _501 + _{carry};
'''
    for i, (lo, hi) in enumerate(((500, 501), (504, 507), (508, 511), (512, 515))):
        low, high, out = 600 + i * 3, 601 + i * 3, 602 + i * 3
        prefix += f'''    int _{low} = _{lo};
    int _{high} = _{hi};
    int _{out} = _900(_{low}, _{high}, (*spvDescriptorSet0.cp_s5));
'''
    return prefix + '}\n'


class RawLoadQuads(unittest.TestCase):
    def test_opt_in_changes_chain(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(dmsh_chain.raw_load_quads())
            command = [sys.executable, dmsh_chain.__file__]
            original = subprocess.check_output(command)
            os.environ['DMSH_RAW_LOAD_QUADS'] = '1'
            self.assertTrue(dmsh_chain.raw_load_quads())
            self.assertNotEqual(subprocess.check_output(command), original)

    def test_known_layouts(self):
        for entries in (4096, 8192):
            with self.subTest(entries=entries):
                stock = raw_source(entries)
                result = raw_load_quads(stock, 5, entries, 32)
                self.assertIn(f'index < {entries - 1}u ?', result)
                self.assertIn('next > address + 15ul', result)
                self.assertIn('end - address >= 16ul', result)
                self.assertIn('dm_raw_load4(_600, _601, (*spvDescriptorSet0.cp_s5))', result)
                for ident, component in zip((602, 605, 608, 611), 'xyzw'):
                    self.assertIn(f'int _{ident} = _602_words.{component};', result)
                self.assertEqual(result.count('return _900(low, high, table);'), 1)
                self.assertEqual(result.count('_words = dm_raw_load4'), 1)
                self.assertEqual(raw_load_quads(result, 5, entries, 32), result)

    def test_unrecognized_layout_stays_scalar(self):
        stock = raw_source(4096)
        for slot, entries, entry_bytes in ((4, 4096, 32), (5, 8192, 32), (5, 4096, 16), (5, 7, 32)):
            self.assertEqual(raw_load_quads(stock, slot, entries, entry_bytes), stock)
        for old, new in (('uint _m0[1]', 'float _m0[1]'),
                         ('device _10* cp_s5 [[id(7)]]', 'device _11* cp_s5 [[id(7)]]'),
                         ('return 0;', 'return 1;'),
                         ('< uint(_2);', '<= uint(_2);'),
                         ('int _320 = _310 + 4;', 'int _320 = _310 + 5;')):
            bad = stock.replace(old, new, 1)
            self.assertNotEqual(bad, stock)
            self.assertEqual(raw_load_quads(bad, 5, 4096, 32), bad)

    def test_unproven_words_stay_scalar(self):
        stock = raw_source(4096)
        for old, new in (('int _504 = _500 + 4;', 'int _504 = _500 + 6;'),
                         ('uint(_504) < uint(_500)', 'uint(_504) <= uint(_500)'),
                         ('int _500 = int(id);', 'float _500 = float(id);'),
                         ('int _603 = _504;', 'unknown_effect(_504);\n    int _603 = _504;'),
                         ('int _603 = _504;', 'threadgroup_barrier(mem_flags::mem_device);\n    int _603 = _504;'),
                         ('int _603 = _504;', 'table[0] = 7;\n    int _603 = _504;'),
                         ('int _600 = _500;', 'int _999 = (_500 += 1);\n    int _600 = _500;'),
                         ('int _600 = _500;', 'int _999 = unknown_effect(_500);\n    int _600 = _500;'),
                         ('_900(_603, _604, (*spvDescriptorSet0.cp_s5))',
                          '_900(_603, _604, (*spvDescriptorSet0.cp_s3))')):
            bad = stock.replace(old, new, 1)
            with self.subTest(change=new):
                self.assertNotEqual(bad, stock)
                self.assertEqual(raw_load_quads(bad, 5, 4096, 32), bad)



# The directory lookup (raw_fast_lookup) against the translator's search. A table as the host lays it out (nvn_mtl.m gmem_map_gpu): entry 0 empty, then disjoint ranges
# sorted by start (one starting at the previous end now and then, dense clusters, a few far apart, some crossing 4 GB boundaries), unused entries starting at ~0
E, DIR_LOG2, MASK = dmsh_abi.GMEM_MAP_ENTRIES, dmsh_abi.GMEM_DIR_LOG2, (1 << 64) - 1
HOST = Path(__file__).resolve().parents[1] / 'nvn' / 'nvn_mtl.m'


def random_table(rng, count):
    """[(start, end, data offset)] of count ranges, the data offsets packed one after another."""
    ranges, at, off = [], rng.randrange(1 << 20, 1 << 36) & ~0xfff, 0
    for _ in range(count):
        at += rng.choice((0, 0, 4, 0x1000, 0x10000, rng.randrange(1, 1 << 24), rng.randrange(1, 1 << 34))) & ~3
        size = rng.choice((16, 64, 0x1000, rng.randrange(4, 1 << 16))) & ~3 or 4
        ranges.append((at, at + size, off))
        at += size; off += size
    return ranges


def dir_fill(starts, n):
    """nvn_mtl.m gmem_dir_fill: (base, shift, dir)."""
    base, top, sh = (starts[1], starts[n - 1], 0) if n > 1 else (0, 0, 0)
    while sh < 63 and (top - base) >> sh >= 1 << DIR_LOG2: sh += 1
    out, i = [], 0
    for k in range((1 << DIR_LOG2) + 1):
        x = base + (k << sh)
        x = MASK if x > MASK else x
        while i + 1 < n and starts[i + 1] <= x: i += 1
        out.append(i)
    return base, sh, out + [n - 1]


def old_lookup(starts, ends, a):   # the translator's search: the last start at or below a, then a < end
    i, step = 0, E // 2
    while step:
        i = i + step if starts[i + step] <= a else i
        step //= 2
    return i if a < ends[i] else None


def new_lookup(starts, ends, head, directory, a):   # dmsh_wide.py dm_gmem_index, then dm_gmem_entry
    base, shift = head
    if a < base: i = 0
    else:
        bucket = min((a - base) >> (shift & 63), 1 << DIR_LOG2)
        i = min(directory[bucket], E - 1)
        last = min(max(directory[bucket + 1], i), E - 1)
        width = last - i
        step = 1 << (width.bit_length() - 1) if width else 0
        while step:
            probe = min(i + step, last)
            i = probe if starts[probe] <= a else i
            step >>= 1
    return i if starts[i] <= a < ends[i] else None


def probe_addresses(rng, ranges, count):
    picks = [0, 4, MASK & ~3, (1 << 32) - 4, 1 << 32]
    for start, end, _ in ranges:
        picks += [start, end - 4, end, start - 4]
    while len(picks) < count:
        start, end, _ = rng.choice(ranges)
        picks.append(rng.choice((rng.randrange(start, end), rng.randrange(max(start - 0x10000, 0), end + 0x10000), rng.randrange(0, 1 << 40))) & ~3)
    return [a & MASK for a in picks]


class RawFastLookup(unittest.TestCase):
    def test_opt_in_changes_chain(self):
        with patch.dict(os.environ, {}, clear=True):
            command = [sys.executable, dmsh_chain.__file__]
            original = subprocess.check_output(command)
            os.environ['DMSH_RAW_FAST_LOOKUP'] = '1'
            self.assertTrue(dmsh_chain.raw_fast_lookup())
            self.assertNotEqual(subprocess.check_output(command), original)

    def test_cpu_reference_matches_search(self):
        import random
        rng = random.Random(1405)
        for count in (0, 1, 2, 3, 40, 700, E - 1):
            ranges = random_table(rng, count)
            starts = [0] + [r[0] for r in ranges] + [MASK] * (E - 1 - count)
            ends = [0] + [r[1] for r in ranges] + [0] * (E - 1 - count)
            base, shift, directory = dir_fill(starts, count + 1)
            with self.subTest(count=count, shift=shift, widest=max(b - a for a, b in zip(directory, directory[1:]))):
                for a in probe_addresses(rng, ranges or [(1 << 20, 1 << 21, 0)], 4000):
                    self.assertEqual(new_lookup(starts, ends, (base, shift), directory, a), old_lookup(starts, ends, a), hex(a))
            # A wrong directory may lose an access, never move it to another range
            broken = [rng.randrange(E + 8) if rng.random() < 0.3 else x for x in directory]
            for a in probe_addresses(rng, ranges or [(1 << 20, 1 << 21, 0)], 2000):
                self.assertIn(new_lookup(starts, ends, (base, shift), broken, a), (old_lookup(starts, ends, a), None), hex(a))

    def test_rewrites_known_layouts(self):
        for entries in (4096, 8192):
            with self.subTest(entries=entries):
                stock = raw_source(entries)
                result = raw_fast_lookup(stock, 5, entries, 32, DIR_LOG2)
                self.assertNotIn(f'= 0 + {entries // 2};', result)
                self.assertEqual(result.count('uint _dmg0_i = dm_gmem_index(dm_gmem_address(_1, _2), cp_s5._m0);'), 1)
                self.assertIn('uint4 _dmg0 = dm_gmem_entry(dm_gmem_address(_1, _2), _dmg0_i, cp_s5._m0);', result)
                self.assertIn(f'table + {entries * 8}u', result)
                for word, lane in (('_312', 'x'), ('_314', 'y'), ('_321', 'z'), ('_326', 'w')):
                    self.assertIn(f'float {word} = as_type<float>(_dmg0.{lane});', result)
                self.assertNotIn('cp_s5._m0[', result.split('int _900(')[1].split('kernel void')[0])
                self.assertNotIn('int _310 =', result)
                self.assertEqual(raw_fast_lookup(result, 5, entries, 32, DIR_LOG2), result)

    def test_with_quads(self):
        quads = raw_load_quads(raw_source(8192), 5, 8192, 32, fast=True)
        self.assertIn('dm_raw_word_pair(table, word) <= address && end >= address', quads)
        self.assertIn('reinterpret_cast<const device ulong*>(table._m0)[word >> 1u]', quads)
        result = raw_fast_lookup(quads, 5, 8192, 32, DIR_LOG2)
        self.assertEqual(result.count('= dm_gmem_index('), 2)   # the scalar helper's and dm_raw_find's
        self.assertIn('uint _dmg1_i = dm_gmem_index(dm_gmem_address(_1, _2), cp_s5._m0);\n    int _255 = int(_dmg1_i);\n    return _255;', result)
        self.assertNotIn('dm_raw_word_pair(table, word) <=', raw_load_quads(raw_source(8192), 5, 8192, 32))

    def test_unrecognized_stays(self):
        stock = raw_source(8192)
        for slot, entries, entry_bytes, log2 in ((4, 8192, 32, DIR_LOG2), (5, 4096, 32, DIR_LOG2), (5, 8192, 16, DIR_LOG2), (5, 7, 32, DIR_LOG2), (5, 8192, 32, 0)):
            self.assertEqual(raw_fast_lookup(stock, slot, entries, entry_bytes, log2), stock)
        for old, new in (('<= uint(_1);', '< uint(_1);'), ('int _100 = 0 + 4096;', 'int _100 = 0 + 4095;'),
                         ('_m0[_102]', '_m0[_104]'), ('static inline __attribute__((always_inline))\nint _900', 'int _900')):
            bad = stock.replace(old, new, 1)
            with self.subTest(change=new):
                self.assertNotEqual(bad, stock)
                self.assertEqual(raw_fast_lookup(bad, 5, 8192, 32, DIR_LOG2), bad)
        tail = stock.replace('int _311 = _310 + 2;', 'int _311 = _310 + 6;', 1)   # the search still goes, the entry's words stay as they are
        result = raw_fast_lookup(tail, 5, 8192, 32, DIR_LOG2)
        self.assertIn('int _310 = _255 * 8;', result)
        self.assertIn('float _312 = as_type<float>(cp_s5._m0[_311]);', result)
        self.assertIn('float _321 = as_type<float>(cp_s5._m0[_320]);', result)

    @unittest.skipUnless(shutil.which('xcrun') and shutil.which('clang') and sys.platform == 'darwin', 'Metal')
    def test_gpu_matches_search(self):
        """The MSL itself on this Mac's GPU, the directory written by the host's own gmem_dir_fill: the stock search, its directory form, and the shared reads of four words."""
        import random
        rng = random.Random(7)
        for count in (3000, E - 1):
            with self.subTest(count=count):
                self.gpu_case(rng, count)

    def gpu_case(self, rng, count):
        import struct
        from bisect import bisect_right
        ranges = random_table(rng, count)
        addresses = probe_addresses(rng, ranges, 40000)
        data_words = (ranges[-1][2] + ranges[-1][1] - ranges[-1][0]) // 4 + 1
        table = bytearray(E * 32)
        for k in range(1, E): struct.pack_into('<Q', table, 32 * k, MASK)
        for k, (start, end, off) in enumerate(ranges, 1): struct.pack_into('<3Q', table, 32 * k, start, end, off)

        starts = [r[0] for r in ranges]

        def word(a):   # what a scalar read of a gives: the data buffer's word, or 0 outside every range
            start, end, off = ranges[max(bisect_right(starts, a) - 1, 0)]
            return (off + a - start) // 4 * 2654435761 % (1 << 32) | 1 if start <= a < end else 0
        expect = [[word(a + 4 * k) if a + 4 * k <= MASK else 0 for k in range(4)] for a in addresses]
        host = HOST.read_text()
        fill = host[host.index('enum { GMEM_DIR_N'):host.index('static uint64_t gmem_map_gpu(void) {\n    uint64_t gen')]
        stock = raw_source(8192)
        variants = {'search': (stock, 1), 'fast': (raw_fast_lookup(stock, 5, 8192, 32, DIR_LOG2), 1),
                    'quads': (raw_load_quads(stock, 5, 8192, 32), 4),
                    'fast quads': (raw_fast_lookup(raw_load_quads(stock, 5, 8192, 32, fast=True), 5, 8192, 32, DIR_LOG2), 4)}
        with tempfile.TemporaryDirectory() as d:
            harness = os.path.join(d, 'gmem_gpu')
            Path(harness + '.m').write_text(GPU_HARNESS.replace('/*FILL*/', fill))
            r = subprocess.run(['clang', '-fobjc-arc', '-O1', '-w', f'-DDMSH_GMEM_MAP_ENTRIES={E}', '-DDMSH_GMEM_MAP_ENTRY=32', f'-DDMSH_GMEM_DIR_LOG2={DIR_LOG2}',
                                harness + '.m', '-o', harness, '-framework', 'Metal', '-framework', 'Foundation'], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            Path(d, 'table.bin').write_bytes(table)
            Path(d, 'addr.bin').write_bytes(struct.pack(f'<{len(addresses)}Q', *addresses))
            for name, (msl, lanes) in variants.items():
                kernel = msl[:msl.index('kernel void main0')] + GPU_KERNEL.replace('/*CALL*/', 'int4(_900(lo, hi, cp_s5), 0, 0, 0)' if lanes == 1 else 'dm_raw_load4(lo, hi, cp_s5)')
                Path(d, 'k.metal').write_text('#include <metal_stdlib>\nusing namespace metal;\n' + kernel)
                r = subprocess.run([harness, os.path.join(d, 'k.metal'), os.path.join(d, 'table.bin'), str(len(ranges) + 1), str(data_words),
                                    os.path.join(d, 'addr.bin'), os.path.join(d, 'out.bin')], capture_output=True, text=True)
                with self.subTest(variant=name):
                    self.assertEqual(r.returncode, 0, r.stderr)
                    got = struct.unpack(f'<{4 * len(addresses)}I', Path(d, 'out.bin').read_bytes())
                    bad = [(hex(a), got[4 * i:4 * i + lanes], e[:lanes]) for i, (a, e) in enumerate(zip(addresses, expect)) if list(got[4 * i:4 * i + lanes]) != e[:lanes]]
                    self.assertEqual(bad[:5], [], f'{len(bad)} of {len(addresses)} differ')


GPU_KERNEL = r"""
kernel void gmem_test(device _10& cp_s5 [[buffer(0)]], const device ulong* addresses [[buffer(1)]], device int4* out [[buffer(2)]], const device uint* data [[buffer(3)]],
                      uint id [[thread_position_in_grid]])
{
    uint2 words = as_type<uint2>(addresses[id]);
    int lo = int(words.x), hi = int(words.y);
    out[id] = /*CALL*/;
}
"""

# Runs one kernel over the addresses: the table's data offsets become deltas into a data buffer of words (w * 2654435761) | 1, the directory is the host's gmem_dir_fill
GPU_HARNESS = r"""
#import <Metal/Metal.h>
#include <stdio.h>
#include <string.h>
#define MLOG(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)
/*FILL*/
static NSData *slurp(const char *p) { return [NSData dataWithContentsOfFile:@(p)]; }
int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc != 7) return 2;
        id<MTLDevice> dev = MTLCreateSystemDefaultDevice(); NSError *err = nil;
        NSString *src = [[NSString alloc] initWithData:slurp(argv[1]) encoding:NSUTF8StringEncoding];
        MTLCompileOptions *opt = [MTLCompileOptions new]; opt.mathMode = MTLMathModeRelaxed;
        id<MTLLibrary> lib = [dev newLibraryWithSource:src options:opt error:&err]; if (!lib) { fprintf(stderr, "%s\n", err.description.UTF8String); return 3; }
        id<MTLComputePipelineState> ps = [dev newComputePipelineStateWithFunction:[lib newFunctionWithName:@"gmem_test"] error:&err]; if (!ps) return 4;
        int n = atoi(argv[3]); size_t words = strtoull(argv[4], 0, 10);
        id<MTLBuffer> data = [dev newBufferWithLength:words * 4 + 64 options:MTLResourceStorageModeShared];
        for (size_t w = 0; w < words; w++) ((uint32_t *)data.contents)[w] = (uint32_t)(w * 2654435761u) | 1;
        NSData *tab = slurp(argv[2]);
        id<MTLBuffer> table = [dev newBufferWithLength:DMSH_GMEM_MAP_ENTRIES * DMSH_GMEM_MAP_ENTRY + GMEM_DIR_BYTES options:MTLResourceStorageModeShared];
        uint8_t *t = table.contents; memset(t, 0, table.length); memcpy(t, tab.bytes, tab.length);
        for (int k = 1; k < n; k++) { uint64_t *e = (uint64_t *)(t + 32 * k); e[2] = data.gpuAddress + e[2] - e[0]; }
        gmem_dir_fill(t, n);
        NSData *adr = slurp(argv[5]); size_t count = adr.length / 8;
        id<MTLBuffer> addrs = [dev newBufferWithBytes:adr.bytes length:adr.length options:MTLResourceStorageModeShared];
        id<MTLBuffer> out = [dev newBufferWithLength:count * 16 options:MTLResourceStorageModeShared];
        id<MTLCommandBuffer> cb = [[dev newCommandQueue] commandBuffer];
        id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:ps]; [enc setBuffer:table offset:0 atIndex:0]; [enc setBuffer:addrs offset:0 atIndex:1]; [enc setBuffer:out offset:0 atIndex:2]; [enc setBuffer:data offset:0 atIndex:3];
        [enc useResource:data usage:MTLResourceUsageRead];   // read only through the physical pointers
        [enc dispatchThreads:MTLSizeMake(count, 1, 1) threadsPerThreadgroup:MTLSizeMake(64, 1, 1)]; [enc endEncoding];
        [cb commit]; [cb waitUntilCompleted]; if (cb.error) { fprintf(stderr, "%s\n", cb.error.description.UTF8String); return 5; }
        [[NSData dataWithBytes:out.contents length:count * 16] writeToFile:@(argv[6]) atomically:NO];
    }
    return 0;
}
"""


if __name__ == '__main__':
    unittest.main()
