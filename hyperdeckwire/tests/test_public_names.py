# SPDX-License-Identifier: MIT
"""``resolve_slot_dir`` is public since 1.14.1 (a caller deleting clips over
FTP finds the slot the way an upload does); ``_resolve_slot_dir`` is the
same function under its old name."""
from hyperdeckwire import upload


class _FTP:
    def __init__(self, entries):
        self.entries = entries

    def nlst(self):
        return list(self.entries)


def test_resolve_slot_dir_is_public_and_the_old_name_is_the_same_function():
    assert upload._resolve_slot_dir is upload.resolve_slot_dir


def test_resolve_slot_dir_picks_a_numeric_slot_then_a_named_mount():
    assert upload.resolve_slot_dir(_FTP(['2', '1'])) == '1'
    assert upload.resolve_slot_dir(_FTP(['usb', 'sd1'])) == 'sd1'
    assert upload.resolve_slot_dir(_FTP([]), slot=2) == '2'
