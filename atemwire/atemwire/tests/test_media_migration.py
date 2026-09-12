# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``media`` feature.

Phase 3 commit 4: media pool + media-player setters and readers
moved from atemwire.operations / atemwire.state into atemwire.messages.media.

  Bucket A (4): clear_still, capture_still, set_media_player_still,
                set_media_player_clip. The MediaplayerSelectCommand
                Send class does the mask + source_type bookkeeping
                internally; the wrappers are pure pass-through.
  Bucket A (1 reader): mediaplayer_slots (single field on the slots Recv).
  Bucket B (2 readers): mediaplayer_slot_info (dict of is_used/hash/name
                with md5_hex + decode_name handling),
                mediaplayer_selected (2-field dict).

No scaled fields in media — all identity ints / bytes / bools.

The +8 header offset (Send.get_command prepends 8 bytes) applies.
"""



from atemwire.messages.media import (
    capture_still, clear_still, mediaplayer_selected,
    mediaplayer_slot_info, mediaplayer_slots, set_media_player_clip,
    set_media_player_still
)


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd.get_command())


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------

def test_clear_still_packs_slot_index():
    conn = _FakeConn()
    clear_still(conn, slot=7)
    assert conn.sent[0][4:8] == b'CSTL'
    assert conn.sent[0][8] == 7   # slot u8 at payload offset 0


def test_capture_still_has_no_payload():
    """Capt is a zero-payload command — only the 8-byte header."""
    conn = _FakeConn()
    capture_still(conn)
    assert conn.sent[0][4:8] == b'Capt'
    assert len(conn.sent[0]) == 8


def test_set_media_player_still_packs_mask_source_slot():
    """MPSS sets mask=0b011 (bit0 + bit1=still), source_type=1, still=slot."""
    conn = _FakeConn()
    set_media_player_still(conn, player=0, slot=5)
    payload = conn.sent[0][8:]
    assert payload[0] == 0b011
    assert payload[1] == 0       # player index
    assert payload[2] == 1       # source_type=still
    assert payload[3] == 5       # still slot


def test_set_media_player_clip_packs_mask_source_slot():
    """MPSS sets mask=0b101 (bit0 + bit2=clip), source_type=2, clip=slot."""
    conn = _FakeConn()
    set_media_player_clip(conn, player=1, slot=3)
    payload = conn.sent[0][8:]
    assert payload[0] == 0b101
    assert payload[1] == 1
    assert payload[2] == 2       # source_type=clip
    assert payload[4] == 3       # clip slot


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

class _MediaplayerSlots:
    def __init__(self, stills):
        self.stills = stills


class _MPfe:
    def __init__(self, is_used=False, hash=b'', name=b''):
        self.is_used = is_used
        self.hash = hash
        self.name = name


class _MediaplayerSelected:
    def __init__(self, source_type=0, slot=0):
        self.source_type = source_type
        self.slot = slot


def test_mediaplayer_slots_reads_capacity():
    mx = {'mediaplayer-slots': _MediaplayerSlots(stills=32)}
    assert mediaplayer_slots(mx) == 32
    assert mediaplayer_slots({}) == 0


def test_mediaplayer_slot_info_decodes_name_and_hash():
    """Bytes name → decoded string; non-zero hash bytes → hex string."""
    h = bytes.fromhex('0123456789abcdef0123456789abcdef')
    mx = {'mediaplayer-file-info': {
        0: _MPfe(is_used=True, hash=h, name=b'shot01\x00\x00'),
    }}
    info = mediaplayer_slot_info(mx, 0)
    assert info['is_used'] is True
    assert info['hash'] == h.hex()
    assert info['name'] == 'shot01'


def test_mediaplayer_slot_info_default_when_unpopulated():
    """Empty mixerstate → safe defaults across all three fields."""
    result = mediaplayer_slot_info({}, 0)
    assert result == {'is_used': False, 'hash': '', 'name': ''}


def test_mediaplayer_slot_info_all_zero_hash_renders_empty():
    """An all-zeros 16-byte hash means 'no hash computed yet' → ''."""
    mx = {'mediaplayer-file-info': {
        0: _MPfe(is_used=True, hash=bytes(16), name=b'pending'),
    }}
    assert mediaplayer_slot_info(mx, 0)['hash'] == ''


def test_mediaplayer_selected_reads_source_type_and_slot():
    """MP1 pointing at still slot 4 → {source_type: 1, slot: 4}."""
    mx = {'mediaplayer-selected': {
        0: _MediaplayerSelected(source_type=1, slot=4),
    }}
    result = mediaplayer_selected(mx, 0)
    assert result == {'source_type': 1, 'slot': 4}


def test_mediaplayer_selected_default_when_unpopulated():
    assert mediaplayer_selected({}, 0) == {'source_type': 0, 'slot': 0}
