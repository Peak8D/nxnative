"""NDSR packs are lists of 8-byte items, and NDVE files hold an NDSR pack and an RLA1 container of NVN2 compute shaders. Synthetic bytes only."""
import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path

from dm.tools.tests.test_split_gzip import ss


def nvn2(seed, code_len=0x100, ctl_len=0xe0):
    """An NVN2 compute shader: a control section (magic, version 0xc9, the code length at +0x44, stage byte 5 at +0x56) and its code area"""
    ctl = bytearray(ctl_len); struct.pack_into('<II', ctl, 0, 0x98761234, 0xc9); struct.pack_into('<I', ctl, 0x44, code_len); ctl[0x56] = 5
    struct.pack_into('<3I', ctl, 0x58, 8, 8, 1); ctl[0x60] = seed
    code = bytes([seed]) * code_len
    return bytes(ctl), code


def item(off, length, kind): return struct.pack('<II', off, length << 4 | kind)


def ndsr(shaders, data_between):
    """An NDSR pack of the newer header (+4 0x40100, items from +0x14): one data item first, then per shader a control item and a code item, with data_between data items after each"""
    head = bytearray(b'NDSR' + struct.pack('<4I', 0x40100, 0, 0, 0))
    n_items = 1 + len(shaders) * (2 + data_between) + 1
    body = bytearray(bytes(0x14 + 8 * n_items - len(head)))
    blob = head + body
    blob += bytes(-len(blob) % 0x100)
    items = [item(len(blob), 4, 0xb)]; blob += bytes(4)
    for ctl, code in shaders:
        blob += bytes(-len(blob) % 0x100); c = len(blob); blob += ctl
        blob += bytes(-len(blob) % 0x100); k = len(blob); blob += code
        items += [item(c, len(ctl), 6), item(k, len(code), 7)]
        for _ in range(data_between): items.append(item(len(blob), 4, 9)); blob += bytes(4)
    struct.pack_into(f'{len(items) * 8}s', blob, 0x14, b''.join(items))   # a zero item ends the list
    return bytes(blob)


def rla1(blobs):
    """An RLA1 container: each blob {shader count, 1 data block, (offset, length), shader records}, code after each control section, and the footer that lists the blobs"""
    out, index = bytearray(), []
    for shaders in blobs:
        start = len(out)
        head = 8 + 8 + 16 * len(shaders)
        weights = bytes(0x20)
        b = bytearray(struct.pack('<2I', len(shaders), 1) + struct.pack('<2I', head, len(weights)) + bytes(16 * len(shaders)) + weights)
        for i, (ctl, code) in enumerate(shaders):
            struct.pack_into('<4I', b, 16 + 16 * i, len(b), len(ctl), len(code), 0); b += ctl + code
        out += b; index.append((len(index), start, len(b)))
    foot = len(out)
    out += struct.pack('<2I', 12 + 12 * len(index), len(index)) + b''.join(struct.pack('<3I', *e) for e in index) + struct.pack('<I', foot) + b'1ALR'
    return bytes(out)


def ndve(pack, container):
    return b'NDVE' + struct.pack('<2HII', 5, 0xff0, 0, len(pack)) + struct.pack('<I', len(container)) + bytes(0x14) + pack + container


class NdsrItems(unittest.TestCase):
    def test_odd_data_items_keep_every_shader(self):
        # one data item between the two shaders shifts the second off a 16-byte record grid; the item walk still pairs each control item with the code item after it
        shaders = [nvn2(1), nvn2(2, 0x200)]
        found = ss.ndsr_shaders(ndsr(shaders, 1), 'p')
        self.assertEqual([(st, ctl, code) for _, st, ctl, code in found], [('cs', *s) for s in shaders])

    def test_paired_records_unchanged(self):
        shaders = [nvn2(1), nvn2(2), nvn2(3)]
        found = ss.ndsr_shaders(ndsr(shaders, 0), 'p')
        self.assertEqual([code for *_, code in found], [s[1] for s in shaders])


class Ndve(unittest.TestCase):
    def scan(self, data, name='Movie/Ndve/Ndve-Nvn2-vid.dat'):
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()) as log:
            path = Path(t) / 'source'; path.write_bytes(data)
            result = ss.scan_file((str(path), name))
        return result, log.getvalue()

    def test_both_parts_found_through_the_prefilter(self):
        upscaler = [nvn2(1), nvn2(2)]
        blobs = [[nvn2(3, 0x300, 0x1970), nvn2(4)], [nvn2(5, 0x80, 0x768)]]
        (_, keys, _, extra), log = self.scan(ndve(ndsr(upscaler, 1), rla1(blobs)))
        self.assertEqual(log, ''); self.assertEqual(extra, {})
        want = {ss.skey(ctl, code) for ctl, code in upscaler + [s for b in blobs for s in b]}
        self.assertEqual(set(keys), want)
        self.assertEqual(sum('#NDVE#RLA1@' in v[0] for v in keys.values()), 3)

    def test_sizes_that_do_not_fill_the_file_are_red(self):
        data = bytearray(ndve(ndsr([nvn2(1)], 0), rla1([[nvn2(2)]])))
        struct.pack_into('<I', data, 0x10, 0x40)   # the RLA1 length
        (_, keys, _, extra), log = self.scan(bytes(data))
        self.assertEqual(keys, {}); self.assertIn('NDVE header', log); self.assertEqual(extra[0], 'unreadable')


if __name__ == '__main__':
    unittest.main()
