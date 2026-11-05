"""A BAHS file's binary array lists its entries: a stray match of the entry scan (bytes inside a control section that read as an entry) is no shader. Synthetic bytes only."""
import struct
import unittest

from dm.tools.tests.test_split_gzip import fz, ss


def with_stray():
    """fz.bahs() with an entry-shaped 16 bytes inside the pixel shader's control section: the vertex shader's control section with the pixel shader's code, a pair no record makes"""
    d = bytearray(fz.bahs())
    at = 0x100 + 0x880 + 0x400   # inside the second control section
    struct.pack_into('<IIIi', d, at, 0x100, 0, 0x880, 0x100 - at)
    return bytes(d)


class Strays(unittest.TestCase):
    def keys(self, d):
        ss.BAHS_VAR.clear()
        return [(where, st) for where, st, _, _ in ss.bahs_shaders(d, 'f')]

    def test_listed_entries_only(self):
        self.assertEqual(self.keys(with_stray()), self.keys(fz.bahs()))
        self.assertEqual(len(self.keys(fz.bahs())), 2)

    def test_the_stray_is_what_the_scan_finds_without_the_array(self):
        d = bytearray(with_stray()); struct.pack_into('<I', d, 0x40, 0)   # an array that does not read back: every match is kept, as before
        self.assertEqual(len(self.keys(bytes(d))), 3)


if __name__ == '__main__':
    unittest.main()
