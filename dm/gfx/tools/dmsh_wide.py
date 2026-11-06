"""Storage-buffer reads the translator split into words, read whole again (wide_loads, a step of dmsh_build.py).

Ampere reads a storage buffer 64 or 128 bits at a time (LDG.E.64 / .128); the translator splits each into 32-bit LoadGlobal operations, and spirv-cross writes
every word with an address of its own:

    int _3487 = _3483 + 32;  uint _3499 = uint(int(uint(_3487) >> uint(2)));  float _3501 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3499)]);
    int _3503 = _3487 + 4;   uint _3505 = uint(int(uint(_3503) >> uint(2)));  float _3507 = as_type<float>((*spvDescriptorSet0.fp_s3)._m0[int(_3505)]);

Metal's front end cannot tell that such words are neighbours (each index is the shift of its own sum, then widened), so they stay separate loads, each with its
own address arithmetic. Splatoon Raiders' material shader (Hoian_UBER) reads its lights this way, 80 bytes a light in a loop over the lights, about 240 words in
all: on an M1 the scene's lit pass took 70 ms, 16 ms with the words read as below (median GPU time of the pass, the opening dialogue scene).

Here the words of one read-only buffer (const device) at byte offsets k, k+4, ... from one value, with no brace between them (one block of straight-line code),
become one read of a packed_uint2/3/4 at the first of them, and each word reads its lane. A packed vector needs only 4-byte alignment, so it may sit wherever
the words could; the shader does not write the buffer through its own binding, and a run ends at any line that may write memory or change a value (a store
through a subscript or a pointer, an atomic, a barrier, an image write, an assignment to an existing variable), so a write through another binding to the
same memory, or a common value that changes, cannot fall between its words: reading the later words earlier gives the same values. A run starts on a 16-byte
step of the common value and holds at most four words, as the original wide reads did; a run whose lowest word is not read first is left alone.
"""
import re

_IDENT = r'_[0-9A-Za-z_]+'
_BUF = r'[a-z]p_s\d+'   # storage buffer bindings: vp_ / fp_ / cp_ / gp_ ... s<n>
_ADD = re.compile(r'^\s*int (' + _IDENT + r') = (' + _IDENT + r') \+ (-?\d+);\s*$')
_IDX = re.compile(r'^\s*uint (' + _IDENT + r') = uint\(int\(uint\((' + _IDENT + r')\) >> uint\(2\)\)\);\s*$')
_LOAD = re.compile(r'\(\*spvDescriptorSet0\.(' + _BUF + r')\)\._m0\[int\((' + _IDENT + r')\)\]')
_RO = re.compile(r'^\s*const device ' + _IDENT + r'\* (' + _BUF + r') \[\[id\(\d+\)\]\];', re.M)
_OP = r'(?:[-+*/%|&^]|<<|>>)?=(?!=)'
_WRITE = re.compile(r'atomic_|_barrier\(|\.write\(|\]\s*' + _OP + r'|^\s*\*|^\s*' + _IDENT + r'\s*' + _OP)   # lines a run does not reach across
_VEC = {2: 'packed_uint2', 3: 'packed_uint3', 4: 'packed_uint4'}


def _root(v, adds):   # (value, byte offset) the sums lead back to
    off = 0
    for _ in range(64):
        if v not in adds: break
        v, k = adds[v]; off += k
    return v, off


def wide_loads(msl):
    if ' >> uint(2)' not in msl: return msl
    ro = set(_RO.findall(msl))
    if not ro: return msl
    lines = msl.split('\n')
    adds, idx = {}, {}
    for ln in lines:
        m = _ADD.match(ln)
        if m: adds[m.group(1)] = (m.group(2), int(m.group(3))); continue
        m = _IDX.match(ln)
        if m: idx[m.group(1)] = m.group(2)
    words, block = {}, 0   # (block, buffer, value) → {byte offset: (line, match)}
    for i, ln in enumerate(lines):
        if '{' in ln or '}' in ln or _WRITE.search(ln): block += 1; continue
        ms = list(_LOAD.finditer(ln))
        if len(ms) != 1 or ms[0].group(1) not in ro or ms[0].group(2) not in idx: continue
        v, off = _root(idx[ms[0].group(2)], adds)
        if off % 4 == 0: words.setdefault((block, ms[0].group(1), v), {}).setdefault(off, (i, ms[0]))
    edits, n = {}, 0
    for (_, buf, _), by_off in words.items():
        for base in sorted({off - off % 16 for off in by_off}):
            run = []
            while len(run) < 4 and base + 4 * len(run) in by_off: run.append(by_off[base + 4 * len(run)])
            if len(run) < 2 or any(i < run[0][0] for i, _ in run): continue
            name, vec = f'_dmw{n}', _VEC[len(run)]; n += 1
            decl = f'{vec} {name} = *(const device {vec}*)&(*spvDescriptorSet0.{buf})._m0[int({run[0][1].group(2)})];'
            for w, (i, m) in enumerate(run): edits.setdefault(i, []).append((m.start(), m.end(), f'{name}.{"xyzw"[w]}', decl if w == 0 else None))
    for i, es in edits.items():
        ln, pre = lines[i], []
        for s, e, rep, decl in sorted(es, reverse=True):
            ln = ln[:s] + rep + ln[e:]
            if decl: pre.append(decl)
        indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = ''.join(indent + d + '\n' for d in pre) + ln
    return '\n'.join(lines)


# Unlike a bound storage word, each raw word independently selects a guest-memory
# range. Only reuse that selection when all four accesses choose the same range.
_RAW_ID = r'_\d+'
_RAW_FUNCTION = re.compile(
    r'(?m)^static inline __attribute__\(\(always_inline\)\)\n'
    rf'int (?P<name>{_RAW_ID})\(thread const int& (?P<lo>{_RAW_ID}), '
    rf'thread const int& (?P<hi>{_RAW_ID}), device (?P<type>{_RAW_ID})& (?P<table>{_BUF})\)\n'
    r'\{\n(?P<body>.*?)^\}', re.S)


def _raw_search(body, low, high, table, entries, stride):
    """Verify the fixed-table scalar helper and return its unmodified search prefix."""
    ident = _RAW_ID
    lines = iter(body.splitlines())
    prefix = []
    names = {'lo': low, 'hi': high, 'table': table, 'stride': str(stride), 'previous': '0'}

    def match(template, *, search=False):
        line = next(lines, '')
        pattern = re.sub(r'@([a-z_]+)', lambda m: re.escape(names[m[1]]) if m[1] in names else
                         rf'(?P<{m[1]}>{ident})', template)
        result = re.fullmatch(pattern, line.strip())
        if not result:
            return False
        names.update(result.groupdict())
        if search:
            prefix.append(line)
        return True

    step_lines = (
        r'int @probe = @previous \+ @step;',
        r'int @row = @probe \* @stride;',
        r'int @lindex = @row \+ 0;',
        r'float @lbits = as_type<float>\(@table\._m0\[@lindex\]\);',
        r'int @hindex = @row \+ 1;',
        r'float @hbits = as_type<float>\(@table\._m0\[@hindex\]\);',
        r'bool @lower = as_type<uint>\(@hbits\) < uint\(@hi\);',
        r'bool @equal = as_type<int>\(@hbits\) == @hi;',
        r'bool @low_le = as_type<uint>\(@lbits\) <= uint\(@lo\);',
        r'bool @same = @equal && @low_le;',
        r'bool @take = @lower \|\| @same;',
        r'int @index = @take \? @probe : @previous;',
    )
    step = entries // 2
    while step:
        for name in ('probe', 'row', 'lindex', 'lbits', 'hindex', 'hbits',
                     'lower', 'equal', 'low_le', 'same', 'take', 'index'):
            names.pop(name, None)
        names['step'] = str(step)
        if not all(match(line, search=True) for line in step_lines):
            return None
        names['previous'] = names['index']
        step //= 2
    del names['row']
    tail = (
        r'int @row = @index \* @stride;',
        r'int @end_l = @row \+ 2;',
        r'float @end_lb = as_type<float>\(@table\._m0\[@end_l\]\);',
        r'int @end_h = @row \+ 3;',
        r'float @end_hb = as_type<float>\(@table\._m0\[@end_h\]\);',
        r'bool @end_high = uint\(@hi\) < as_type<uint>\(@end_hb\);',
        r'bool @end_equal = @hi == as_type<int>\(@end_hb\);',
        r'bool @end_low = uint\(@lo\) < as_type<uint>\(@end_lb\);',
        r'bool @end_same = @end_equal && @end_low;',
        r'bool @inside = @end_high \|\| @end_same;',
        r'if \(@inside\)',
        r'\{',
        r'int @delta_l = @row \+ 4;',
        r'float @delta_lb = as_type<float>\(@table\._m0\[@delta_l\]\);',
        r'int @host_l = @lo \+ as_type<int>\(@delta_lb\);',
        r'bool @wrap = uint\(@host_l\) < uint\(@lo\);',
        r'int @carry = int\(@wrap\);',
        r'int @delta_h = @row \+ 5;',
        r'float @delta_hb = as_type<float>\(@table\._m0\[@delta_h\]\);',
        r'int @high_sum = @hi \+ as_type<int>\(@delta_hb\);',
        r'int @host_h = @high_sum \+ @carry;',
        r'uint @word = \*\(reinterpret_cast<device uint\*>\(as_type<ulong>\(uint2\(uint\(@host_l\), uint\(@host_h\)\)\)\)\);',
        r'float @bits = as_type<float>\(@word\);',
        r'return as_type<int>\(@bits\);',
        r'\}',
        r'return 0;',
    )
    if not all(match(line) for line in tail) or next(lines, None) is not None:
        return None
    return '\n'.join(prefix), names['index']


def _raw_address(lines, low, high):
    """Find the inputs of the two plain address copies immediately before a read."""
    low_alias, high_alias = lines
    low_alias = re.fullmatch(rf'\s*int {low} = ({_RAW_ID});', low_alias)
    high_alias = re.fullmatch(rf'\s*int {high} = ({_RAW_ID});', high_alias)
    if low_alias and high_alias:
        return low_alias[1], high_alias[1]
    return None


def _raw_quads(msl, helper, table):
    call = re.compile(rf'^(?P<indent> +)int (?P<out>{_RAW_ID}) = {helper}\('
                      rf'(?P<lo>{_RAW_ID}), (?P<hi>{_RAW_ID}), \(\*spvDescriptorSet0\.{table}\)\);$')
    lines = msl.split('\n')
    assignments = {}
    changed = set()
    safe_line = []
    builtin = re.compile(r'\b(?:as_type<(?:float|int|uint)>|int|uint|float|fma|extract_bits|precise::clamp|spvMulExtended<_\d+, uint>)\(')
    call_token = re.compile(r'\b\w+\s*(?:<[^;]+>)?\(')
    for i, line in enumerate(lines):
        declaration = re.fullmatch(rf'\s*(int|bool|uint|float) ({_RAW_ID}) = ([^;]+);', line)
        expression = declaration[3] if declaration else line
        pure = declaration and not call_token.search(builtin.sub('(', expression)) and not re.search(r'(?<![=!<>])' + _OP, expression)
        safe_line.append(bool(pure or call.fullmatch(line)))
        if declaration:
            typ, name, _ = declaration.groups()
            if name in assignments:
                changed.add(name)
            assignments[name] = (i, expression, typ)
        for write in re.finditer(rf'({_RAW_ID})\s*{_OP}|(?:\+\+|--)\s*({_RAW_ID})|({_RAW_ID})\s*(?:\+\+|--)', expression):
            changed.add(next(name for name in write.groups() if name))
        changed.update(re.findall(rf'(?<!\w)&\s*({_RAW_ID})\b', line))

    def expr(name):
        return assignments.get(name, (-1, '', ''))[1] if name not in changed else ''

    def same_address(pair, base, offset, line):
        low, high = pair
        base_low, base_high = base
        addition = re.fullmatch(rf'{base_high} \+ ({_RAW_ID})', expr(high))
        carry = re.fullmatch(rf'int\(({_RAW_ID})\)', expr(addition[1])) if addition else None
        names = (low, high, base_low, base_high)
        if not carry or not addition:
            return False
        names += (carry[1], addition[1])
        declarations = [assignments.get(name, (-1, '', '')) for name in names]
        indices = [entry[0] for entry in declarations]
        types = [entry[2] for entry in declarations]
        return (types == ['int', 'int', 'int', 'int', 'bool', 'int']
                and not any(name in changed for name in names) and min(indices) >= 0
                and max(indices) < line and expr(low) == f'{base_low} + {offset}'
                and expr(carry[1]) == f'uint({low}) < uint({base_low})'
                and all(safe_line[min(indices):line]))

    edits = []
    for start in range(2, len(lines) - 9):
        first = call.fullmatch(lines[start])
        if not first:
            continue
        calls = [first]
        base = _raw_address(lines[start - 2:start], first['lo'], first['hi']) or (first['lo'], first['hi'])
        for i in range(1, 4):
            pos = start + i * 3
            next_call = call.fullmatch(lines[pos])
            if next_call is None or next_call['indent'] != first['indent']:
                break
            pair = _raw_address(lines[pos - 2:pos], next_call['lo'], next_call['hi'])
            if pair is None or not same_address(pair, base, 4 * i, pos):
                break
            calls.append(next_call)
        else:
            edits.append((start, calls))
    for start, calls in edits:
        first = calls[0]
        result = f'{first["out"]}_words'
        load = f'dm_raw_load4({first["lo"]}, {first["hi"]}, (*spvDescriptorSet0.{table}))'
        for i, c in enumerate(calls):
            lines[start + 3 * i] = f'{c["indent"]}int {c["out"]} = {result}.{"xyzw"[i]};'
        lines[start] = f'{first["indent"]}int4 {result} = {load};\n{lines[start]}'
    return '\n'.join(lines) if edits else msl


def raw_load_quads(msl, table_slot, entries, entry_bytes, fast=False):
    """Share the raw pool lookup for adjacent, independently resolved 32-bit loads.

    fast: raw_fast_lookup runs after this pass. Its index may come from the host's
    directory, so the shared read also checks the entry's start and reads the
    table's 64-bit words in one load each."""
    if entry_bytes != 32 or entries < 2 or entries & (entries - 1):
        return msl
    table = f'cp_s{table_slot}'
    for scalar in _RAW_FUNCTION.finditer(msl):
        # The spelling of the table is verified again at the call sites. The
        # caller supplies its slot from the translator's raw-map reflection.
        if scalar['table'] != table:
            continue
        typ = scalar['type']
        struct = rf'struct {typ}\s*\{{\s*uint _m0\[1\];\s*\}};'
        member = rf'(?m)^\s*device {typ}\* {table} \[\[id\(\d+\)\]\];'
        if not re.search(struct, msl) or not re.search(member, msl):
            continue
        stride = entry_bytes // 4
        search = _raw_search(scalar['body'], scalar['lo'], scalar['hi'], table, entries, stride)
        if not search:
            continue
        following = msl[scalar.end():]
        output = _raw_quads(following, scalar['name'], table)
        if output == following:
            continue
        prefix, index = search
        return msl[:scalar.end()] + _raw_quad_helper(scalar, prefix, index, stride, entries, fast) + output
    return msl


def _raw_quad_helper(scalar, search, index, stride, entries, fast=False):
    table, typ, name, low, high = (scalar[k] for k in ('table', 'type', 'name', 'lo', 'hi'))
    pair = ('reinterpret_cast<const device ulong*>(table._m0)[word >> 1u]' if fast else
            'as_type<ulong>(uint2(table._m0[word], table._m0[word + 1u]))')
    start = 'dm_raw_word_pair(table, word) <= address && ' if fast else ''
    return f'''

static inline __attribute__((always_inline))
int dm_raw_find(int {low}, int {high}, device {typ}& {table})
{{
{search}
    return {index};
}}

static inline __attribute__((always_inline))
ulong dm_raw_word_pair(device {typ}& table, uint word)
{{
    return {pair};
}}

static inline __attribute__((always_inline))
int dm_raw_load_word(ulong address, device {typ}& table)
{{
    uint2 words = as_type<uint2>(address);
    int low = as_type<int>(words.x);
    int high = as_type<int>(words.y);
    return {name}(low, high, table);
}}

static inline __attribute__((always_inline))
int4 dm_raw_load4(int low, int high, device {typ}& table)
{{
    const ulong limit = ~0ul - 15ul;
    const ulong address = as_type<ulong>(uint2(as_type<uint>(low), as_type<uint>(high)));
    if (!(address & 3ul) && address <= limit) {{
        const uint index = uint(dm_raw_find(low, high, table));
        const uint word = {stride}u * index;
        const ulong end = dm_raw_word_pair(table, word + 2u);
        // The scalar search prefers the nearest preceding start, even in an overlap.
        const ulong next = index < {entries - 1}u ? dm_raw_word_pair(table, word + {stride}u) : ~0ul;
        if ({start}end >= address && end - address >= 16ul && next > address + 15ul) {{
            const ulong gpu_address = address + dm_raw_word_pair(table, word + 4u);
            if (!(gpu_address & 3ul) && gpu_address <= limit) {{
                const device uint* data = reinterpret_cast<const device uint*>(gpu_address);
                const int x = as_type<int>(data[0]);
                const int y = as_type<int>(data[1]);
                const int z = as_type<int>(data[2]);
                const int w = as_type<int>(data[3]);
                return int4(x, y, z, w);
            }}
        }}
    }}
    const int x = dm_raw_load_word(address, table);
    const int y = dm_raw_load_word(address + 4ul, table);
    const int z = dm_raw_load_word(address + 8ul, table);
    const int w = dm_raw_load_word(address + 12ul, table);
    return int4(x, y, z, w);
}}
'''


# The raw pool lookup through the host's directory (raw_fast_lookup). The translator's
# search reads both 32-bit halves of each start in thirteen dependent steps (8,192
# entries), then the end and, inside the pool, the delta: about 30 loads, 15 of them
# dependent, for each access. The pool table's words are 64-bit values at 8-byte offsets
# of a buffer the host allocates, so they can be read whole, and the host writes a
# directory after the entries (dmsh_abi.h DMSH_GMEM_DIR_*, nvn_mtl.m gmem_dir_fill):
# {base, shift}, then for each bucket k of 1 << shift bytes from base the last entry
# whose start is at or below the bucket's first address. An address in bucket k lies in
# entries dir[k] .. dir[k + 1]; usually that window is one entry wide, and the search
# takes as many steps as the window needs. The entry's start, end and delta come from
# two loads, and an entry starting above the address reads as a miss, so a directory
# that is wrong can drop an access but never move it to another pool.
_GMEM_STEP = (
    r'int @probe = @previous \+ @step;',
    r'int @row = @probe \* @stride;',
    r'int @lindex = @row \+ 0;',
    r'float @lbits = as_type<float>\(@table\._m0\[@lindex\]\);',
    r'int @hindex = @row \+ 1;',
    r'float @hbits = as_type<float>\(@table\._m0\[@hindex\]\);',
    r'bool @lower = as_type<uint>\(@hbits\) < uint\(@hi\);',
    r'bool @equal = as_type<int>\(@hbits\) == @hi;',
    r'bool @low_le = as_type<uint>\(@lbits\) <= uint\(@lo\);',
    r'bool @same = @equal && @low_le;',
    r'bool @take = @lower \|\| @same;',
    r'int @index = @take \? @probe : @previous;',
)
_GMEM_END = (
    r'int @row = @index \* @stride;',
    r'int @end_l = @row \+ 2;',
    r'float @end_lb = as_type<float>\(@table\._m0\[@end_l\]\);',
    r'int @end_h = @row \+ 3;',
    r'float @end_hb = as_type<float>\(@table\._m0\[@end_h\]\);',
)
_GMEM_SAME_STEP = ('probe', 'row', 'lindex', 'lbits', 'hindex', 'hbits', 'lower', 'equal', 'low_le', 'same', 'take', 'index')


def _gmem_match(lines, at, templates, names):
    """Match templates against lines[at:]; @name binds once (fixed when names has it). Returns the bindings or None."""
    names = dict(names)
    for k, template in enumerate(templates):
        if at + k >= len(lines):
            return None
        pattern = re.sub(r'@([a-z_]+)', lambda m: re.escape(names[m[1]]) if m[1] in names else
                         rf'(?P<{m[1]}>{_RAW_ID if m[1] != "table" else _BUF})', template)
        result = re.fullmatch(pattern, lines[at + k].strip())
        if not result:
            return None
        names.update(result.groupdict())
    return names


def _gmem_search(lines, at, table, entries, stride):
    """The translator's whole fixed-table search starting at lines[at]: its bindings (lo, hi, index), or None."""
    names = {'table': table, 'stride': str(stride), 'previous': '0'}
    step = entries // 2
    while step:
        for name in _GMEM_SAME_STEP:
            names.pop(name, None)
        names['step'] = str(step)
        names = _gmem_match(lines, at, _GMEM_STEP, names)
        if names is None:
            return None
        names['previous'] = names['index']
        at += len(_GMEM_STEP)
        step //= 2
    return names


def _gmem_helpers(entries, stride, dir_log2):
    words = entries * stride   # the directory's first 32-bit word: {base, shift}, then the buckets
    return f'''
// nxnative: the raw pool lookup through the host's directory (dmsh_wide.py raw_fast_lookup)
static inline __attribute__((always_inline))
ulong dm_gmem_address(int lo, int hi)
{{
    return as_type<ulong>(uint2(uint(lo), uint(hi)));
}}

static inline __attribute__((always_inline))
uint dm_gmem_index(ulong address, const device uint* table)
{{
    const device ulong* words = reinterpret_cast<const device ulong*>(table);
    const ulong2 head = *reinterpret_cast<const device ulong2*>(table + {words}u);
    if (address < head.x) {{
        return 0u;
    }}
    const uint bucket = uint(min((address - head.x) >> (head.y & 63ul), {1 << dir_log2}ul));
    const packed_uint2 window = *reinterpret_cast<const device packed_uint2*>(table + ({words + 4}u + bucket));
    uint index = min(window.x, {entries - 1}u);
    const uint last = clamp(window.y, index, {entries - 1}u);
    const uint width = last - index;
    for (uint step = width != 0u ? 1u << (31u - clz(width)) : 0u; step != 0u; step >>= 1u) {{
        const uint probe = min(index + step, last);
        index = words[probe * {stride // 2}u] <= address ? probe : index;
    }}
    return index;
}}

static inline __attribute__((always_inline))
uint4 dm_gmem_entry(ulong address, uint index, const device uint* table)
{{
    const device ulong* entry = reinterpret_cast<const device ulong*>(table) + index * {stride // 2}u;
    const ulong2 range = *reinterpret_cast<const device ulong2*>(entry);
    const ulong delta = entry[2];
    return range.x <= address ? uint4(as_type<uint2>(range.y), as_type<uint2>(delta)) : uint4(0u);
}}
'''


def raw_fast_lookup(msl, table_slot, entries, entry_bytes, dir_log2):
    """Replace each verified fixed-table raw pool search on the given table with the directory lookup."""
    if entry_bytes != 32 or entries < 2 or entries & (entries - 1) or not 0 < dir_log2 < 31 or 'dm_gmem_index(' in msl:
        return msl
    stride = entry_bytes // 4
    first_step = re.compile(rf'^\s*int {_RAW_ID} = 0 \+ {entries // 2};$')
    tables = set(re.findall(rf'\b([a-z]p_s{table_slot})\._m0\[', msl))
    if len(tables) != 1:
        return msl
    table = tables.pop()
    lines = msl.split('\n')
    out, k, n, first_function, dead = [], 0, 0, None, []
    function_start = None   # where in out the current function's attribute line is
    while k < len(lines):
        line = lines[k]
        if line == 'static inline __attribute__((always_inline))':
            function_start = len(out)
        if line.startswith('}'):
            function_start = None
        names = _gmem_search(lines, k, table, entries, stride) if first_step.match(line) else None
        if names is None or function_start is None:
            out.append(line)
            k += 1
            continue
        indent = line[:len(line) - len(line.lstrip())]
        k += len(_GMEM_STEP) * (entries.bit_length() - 1)
        if first_function is None:
            first_function = function_start
        lo, hi, index, tag = names['lo'], names['hi'], names['index'], f'_dmg{n}'
        n += 1
        address = f'dm_gmem_address({lo}, {hi})'
        out.append(f'{indent}uint {tag}_i = dm_gmem_index({address}, {table}._m0);')
        dead.append(len(out))
        out.append(f'{indent}int {index} = int({tag}_i);')
        tail = _gmem_match(lines, k, _GMEM_END, {key: v for key, v in names.items() if key not in _GMEM_SAME_STEP[:-1]})
        if tail is None:
            continue
        dead.append(len(out))
        out.append(lines[k])   # the entry's row, which other words of the entry may still use
        out.append(f'{indent}uint4 {tag} = dm_gmem_entry({address}, {tag}_i, {table}._m0);')
        out.append(f'{indent}float {tail["end_lb"]} = as_type<float>({tag}.x);')
        out.append(f'{indent}float {tail["end_hb"]} = as_type<float>({tag}.y);')
        k += len(_GMEM_END)
        # The delta's two words, wherever the function reads them (after the write log of a store or an atomic)
        row = re.escape(tail['row'])
        j = k
        while j + 1 < len(lines) and not lines[j].startswith('}'):
            word = re.fullmatch(rf'(\s*)int ({_RAW_ID}) = {row} \+ ([45]);', lines[j])
            load = word and re.fullmatch(rf'\s*float ({_RAW_ID}) = as_type<float>\({re.escape(table)}\._m0\[{word[2]}\]\);', lines[j + 1])
            if load:
                lines[j] = None
                lines[j + 1] = f'{word[1]}float {load[1]} = as_type<float>({tag}.{"z" if word[3] == "4" else "w"});'
            j += 1
        lines[k:j] = [x for x in lines[k:j] if x is not None]
    if first_function is None:
        return msl
    # The search's own index and the entry's row, when nothing else in their function uses them (last first, so the earlier positions hold)
    for at in sorted(dead, reverse=True):
        name = re.fullmatch(rf'\s*int ({_RAW_ID}) = [^;]+;', out[at])[1]
        end = next((i for i in range(at, len(out)) if out[i].startswith('}')), len(out))
        if sum(len(re.findall(rf'\b{name}\b', x)) for x in out[at:end]) == 1:
            del out[at]
    out.insert(first_function, _gmem_helpers(entries, stride, dir_log2).lstrip('\n'))   # every removed line follows it
    return '\n'.join(out)
