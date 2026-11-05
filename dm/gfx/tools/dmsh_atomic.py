"""Image atomics run natively on the Metal texture (MSL 3.1 texture atomics).

SPIRV-Cross below MSL 3.1 emulates an image atomic with a raw device pointer
beside the texture (<image>_atomic), indexed by the bound texture's width: a
block-linear image is a deswizzled Metal copy whose pool memory has another
layout, so those atomics went to a scratch buffer that nothing read, and a 3D
image did not compile at all (an int3 subscript). Native texture atomics act on
the texture every other access of the image uses, for buffer, 2D, 2D-array and
3D images. The translator still guards each coordinate (IGN bounds) before the
atomic.

Only shaders with image atomics (OpImageTexelPointer) are written at MSL 3.1:
every other shader keeps its MSL 2.3 source byte for byte, so its library comes
from the MSL-keyed compile cache. The emulation must never come back silently:
check_msl fails the shader when SPIRV-Cross writes the pointer form anyway.
"""
import re
import struct

MSL_VERSION = '20300'
MSL_VERSION_ATOMICS = '30100'
OP_IMAGE_TEXEL_POINTER = 60


def uses_image_atomics(spv):
    """Whether the SPIR-V module (path) has an image texel pointer, the operand of every image atomic"""
    with open(spv, 'rb') as f:
        data = f.read()
    if len(data) < 20 or data[:4] != b'\x03\x02\x23\x07':
        raise ValueError('not a SPIR-V module: ' + spv)
    if b'\x3c\x00\x06\x00' not in data:   # the instruction's first word (always 6 words long, opcode 60), nowhere in the module: most shaders, without walking their instructions in Python
        return False
    words = struct.unpack('<%dI' % (len(data) // 4), data[:len(data) // 4 * 4])
    i = 5
    while i < len(words):
        n, op = words[i] >> 16, words[i] & 0xffff
        if op == OP_IMAGE_TEXEL_POINTER:
            return True
        if n == 0:
            raise ValueError('malformed SPIR-V instruction in ' + spv)
        i += n
    return False


def cross_args(xc, spv):
    """spirv-cross arguments for this module: xc with its --msl-version raised to 3.1 when the module has image atomics"""
    if not uses_image_atomics(spv):
        return list(xc)
    args = list(xc)
    args[args.index('--msl-version') + 1] = MSL_VERSION_ATOMICS
    return args


_EMULATED = re.compile(r'\bspvImage2DAtomicCoord\b|device\s+atomic_u?int\s*\*\s*\w+_atomic\b')


def check_msl(source):
    """Fail closed on SPIRV-Cross's pointer emulation of image atomics; return the source with its 64-bit image atomics merged (merge_atomic64)"""
    if 'tomic' in source and _EMULATED.search(source):   # (both alternatives hold the word; the pattern alone tries every character of the source)
        raise ValueError('image atomics emulated through a raw pointer; they must be native texture atomics (MSL 3.1)')
    return merge_atomic64(source)


# 64-bit image atomics. SPIRV-Cross writes none for MSL ("MSL currently does not support 64-bit atomics"), while Metal has MAX and MIN on texture2d<ulong> and
# texture2d_array<ulong> since MSL 3.1 (an RG32Uint texture made with the shader-atomic usage, Apple GPU family 9). Yoshi's Nanite rasterizers (FHWRasterizePS,
# FMicropolyRasterizeCS) pack depth and IDs into one 64-bit visibility buffer texel with SURED.MAX.U64. The translator (InstEmitAmpereMem ASured) sends such a
# reduction as two 32-bit ones on the same texel, the high word first, on the image declared as R32G32Uint (its binding *_rg32ui); here each pair, two guarded
# blocks of the same coordinate expression and operation, becomes the 64-bit atomic, and the image texture2d<ulong>. A half left over fails the shader
_DECL64 = re.compile(r'\b(texture2d(?:_array)?)<uint, access::read_write> (\w+_rg32ui)\b')
_HALF = (r'{ind}(?P<ct{n}>int[23]) (?P<c{n}>_\d+) = (?P<e{n}>[^;\n]+);\n'
         r'(?P=i1)if \(all\((?P<g{n}>[^\n]*)\)\)\n(?P=i1)\{{\n'
         r'(?P=i1)    uint (?P<r{n}>_\d+) = spvDescriptorSet0\.{name}\.atomic_fetch_(?P<op{n}>max|min)\((?P<a{n}>[^\n]*)\)\.x;\n(?P=i1)\}}\n')   # {ind}: the first half's indentation, which the second repeats


def _split_args(s):
    """The top-level comma-separated arguments of a call's argument text"""
    out, depth, cur = [], 0, ''
    for ch in s:
        if ch in '([{': depth += 1
        elif ch in ')]}': depth -= 1
        if ch == ',' and depth == 0: out.append(cur.strip()); cur = ''
        else: cur += ch
    return out + [cur.strip()]


def merge_atomic64(source):
    """Each pair of 32-bit halves of a 64-bit image MAX / MIN (the *_rg32ui images, see above) as one 64-bit texture atomic; ValueError when a half has no partner"""
    for kind, name in sorted(set(_DECL64.findall(source))):
        if not re.search(r'\b%s\.atomic_fetch_' % re.escape(name), source): continue   # an RG32Uint image without atomics (loads and stores) keeps its uint type
        # the second half: the same indentation and coordinate expression, after at most a few plain statements (no blocks, no use of the image)
        pair = re.compile(_HALF.format(n=1, name=re.escape(name), ind=r'(?P<i1>[ \t]*)') + r'(?P<mid>(?:(?P=i1)[^\n{}]*;\n){0,4}?)' + _HALF.format(n=2, name=re.escape(name), ind='(?P=i1)'))
        def one(m):
            if m.group('e1') != m.group('e2') or m.group('op1') != m.group('op2') or m.group('ct1') != m.group('ct2') or name in m.group('mid'):
                return m.group(0)
            a1, a2 = _split_args(m.group('a1')), _split_args(m.group('a2'))
            if a1[:-1] != [x.replace(m.group('c2'), m.group('c1')) for x in a2[:-1]]: return m.group(0)
            i = m.group('i1')
            return (f"{i}{m.group('ct1')} {m.group('c1')} = {m.group('e1')};\n{i}if (all({m.group('g1')}))\n{i}{{\n"
                    f"{i}    spvDescriptorSet0.{name}.atomic_{m.group('op1')}({', '.join(a1[:-1])}, (ulong({a1[-1]}) << 32) | ulong({a2[-1]}));\n{i}}}\n"
                    f"{i}uint {m.group('r1')} = 0u;\n{m.group('mid')}{i}uint {m.group('r2')} = 0u;\n")
        source = pair.sub(one, source)
        if re.search(r'\b%s\.atomic_fetch_' % re.escape(name), source):
            raise ValueError(f'{name}: a half of a 64-bit image atomic without its partner (dmsh_atomic.py merge_atomic64)')
        source = re.sub(r'\b%s<uint, access::read_write> %s\b' % (kind, re.escape(name)), f'{kind}<ulong, access::read_write> {name}', source)
    return source
