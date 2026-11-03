"""The Viper engine's VZIP archives, shader objects and VIPR containers (Star Fox), without game data."""
from pathlib import Path
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import lz4.block
import shader_split as split

SPH = {'vs': 0x020c0481, 'ps': 0x000cd482}


def shader(stage, ncode=0x200):
    ctl, code = bytearray(0xe0), bytearray(ncode)
    ctl[:8] = split.CTL_MAGIC + b'\xc9\0\0\0'
    struct.pack_into('<I', ctl, 0x44, ncode)
    if stage == 'cs': ctl[0x56] = 5
    else: struct.pack_into('<I', code, 0, SPH[stage])
    if stage == 'ps': code[0x18] = 0x0a   # SPH word 6, with no code header before it: the pixel shader reads generic location 0
    code[0x80:0x84] = os.urandom(4)
    return bytes(ctl), bytes(code)


def viper_object(stage):
    ctl, code = shader(stage)
    return struct.pack('<IIII', 0x19866891, len(code), len(ctl), 0) + os.urandom(8) + code + ctl


def vipr_container(*stages, loose=False):
    d = bytearray(b'VIPR' + struct.pack('<I', 2) + bytes(0x40))
    for st in stages:
        ctl, code = shader(st)
        d += os.urandom(8) + struct.pack('<7I', 2, 0, 1, 0x4900, len(ctl), len(code), 0) + ctl + code
    if loose: d += shader('cs')[0]   # a control section in a table, its code area elsewhere
    return bytes(d)


def lz4_chain(d):   # linked blocks of 0x10000 bytes, each compressed against the 0x10000 bytes before it
    out = bytearray()
    for o in range(0, len(d), 0x10000):
        b = lz4.block.compress(d[o:o + 0x10000], store_size=False, dict=d[max(0, o - 0x10000):o])
        out += struct.pack('<I', len(b)) + b
    return bytes(out)


def vzip(members, crc=None):
    """VZIP's layout: its own index first, then local headers (method 0, a data descriptor, sizes 0xffffffff), the members, a central directory with the right sizes and CRCs"""
    d = bytearray(b'VZIP' + os.urandom(0x40)); cd = bytearray()
    for nm, data, packed in members:
        stored = lz4_chain(data) if packed else data
        off = len(d); name = nm.encode()
        d += struct.pack('<IHHHHHIIIHH', 0x04034b50, 45, 8, 0, 0, 0, 0, 0xffffffff, 0xffffffff, len(name), 0) + name + stored
        c = zlib.crc32(data) if crc is None else crc
        cd += struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 45, 45, 8, 0, 0, 0, c, len(stored), len(data), len(name), 0, 0, 0, 0, 0, off) + name
    at = len(d); d += cd
    d += struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(members), len(members), len(cd), at, 0)
    return bytes(d)


class Viper(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)

    def split_zip(self, members, crc=None):
        p = os.path.join(self.dir.name, 'data.zip')
        with open(p, 'wb') as f: f.write(vzip(members, crc))
        jobs, paths = split.zip_jobs([(p, 'data.zip')])
        self.assertEqual(paths, {p})
        return [split.scan_file((p, r, j)) for p, r, j in jobs]

    def test_vzip_members_decode_and_their_shaders_split(self):
        big = os.urandom(0x8000) * 9 + viper_object('vs')   # 0x48000 bytes and more: the blocks after the first take back-references into the one before
        results = self.split_zip([('a', viper_object('ps'), True), ('b', big, True), ('c', vipr_container('cs', 'cs'), True), ('d', b'RIFF' + os.urandom(64), False)])
        found = {k: v for _, keys, _, extra in results if not isinstance(extra, tuple) for k, v in keys.items()}
        self.assertTrue(all(not isinstance(extra, tuple) for *_, extra in results), results)
        self.assertEqual(sorted(st for _, st, _, _ in found.values()), ['cs', 'cs', 'ps'])   # 'b' starts with other bytes: no object
        self.assertTrue(all('#VIPER@' in w for w, *_ in found.values()))

    def test_member_bytes_are_the_decoded_member(self):
        data = os.urandom(0x5000) * 7
        p = os.path.join(self.dir.name, 'data.zip')
        with open(p, 'wb') as f: f.write(vzip([('m', data, True), ('r', b'raw', False)]))
        import zipfile
        with open(p, 'rb') as f, zipfile.ZipFile(f) as z:
            info = {i.filename: i for i in z.infolist()}
            self.assertEqual(split.vzip_member(f, info['m'], 'm'), data)
            self.assertEqual(split.vzip_member(f, info['r'], 'r'), b'raw')

    def test_a_wrong_crc_is_an_unreadable_member(self):
        (_, _, _, extra), = self.split_zip([('m', viper_object('vs'), True)], crc=1)
        self.assertEqual(extra[0], 'unreadable')
        self.assertIn('CRC', extra[1])

    def test_object_layout_and_single_stage_pairing(self):
        found = []
        for st in ('vs', 'ps', 'cs'): split.scan_blob(viper_object(st), f'/data.zip/{st}', found)
        self.assertEqual([s for _, s, _, _ in found], ['vs', 'ps', 'cs'])
        self.assertEqual(split.var_pairs(found), set())
        self.assertEqual(split.ps_inputs(found[1][3]), {0})
        self.assertEqual(len(split.separable_pairs(split.uniq_keys(found))[0]), 1)   # one vertex shader with the one pixel shader's cover
        self.assertEqual(split.viper_shader(viper_object('vs')[:-1], 'short'), [])
        self.assertEqual(split.viper_shader(b'\x91\x68\x86\x19' + bytes(0x100), 'zeros'), [])

    def test_vipr_control_sections_without_code_are_left_out(self):
        out = split.vipr_shaders(vipr_container('cs', loose=True), 'v')
        self.assertEqual([s for _, s, _, _ in out], ['cs'])


if __name__ == '__main__':
    unittest.main()
