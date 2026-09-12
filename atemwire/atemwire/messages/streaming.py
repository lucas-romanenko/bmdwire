# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Streaming state messages — encoder bitrate config, target service config,
live status, and runtime stats.

Wire packets (incoming only):
    STAB — audio bitrate (min / max)
    SRSU — service config (name / URL / key + video bitrate min/max)
    StRS — current stream status enum
    SRSS — runtime stats (bitrate / cache used)
"""

from atemwire.messages._dsl import Recv, i16, string, u16, u32
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


class StreamingAudioBitrateField(Recv):
    """``STAB`` — audio encoder bitrate range.

    Always 128k for both min and max on tested devices.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      4    u32    Min bitrate
    4      4    u32    Max bitrate
    ====== ==== ====== ===========
    """
    CODE = 'STAB'
    PRETTY = 'streaming-audio-bitrate'

    min = u32(at=0)
    max = u32(at=4)

    def __repr__(self):
        return f'<streaming-audio-bitrate min={self.min} max={self.max}>'


class StreamingServiceField(Recv):
    """``SRSU`` — live-stream target service config.

    The video bitrate fields here are shared with the recorder encoder,
    so changing them affects recording quality.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      64   str    Service display name
    64     512  str    Target RTMP URL
    576    512  str    Stream key / secret
    1088   4    u32    Video bitrate min
    1092   4    u32    Video bitrate max
    ====== ==== ====== ===========
    """
    CODE = 'SRSU'
    PRETTY = 'streaming-service'

    name = string(at=0,    size=64)
    url  = string(at=64,   size=512)
    key  = string(at=576,  size=512)
    min  = u32   (at=1088)
    max  = u32   (at=1092)

    def __repr__(self):
        return (f'<streaming-service {self.name} url={self.url} '
                f'min={self.min} max={self.max}>')


class StreamingStatusField(Recv):
    """``StRS`` — live-stream status enum.

    Status values (observed): -1 unknown, 0 nothing, 1 idle,
    2 connecting, 4 on-air, 22/36 stopping.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    i16    Status
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'StRS'
    PRETTY = 'streaming-status'

    status = i16(at=0)

    def __repr__(self):
        return f'<streaming-status status={self.status}>'


class StreamingStatsField(Recv):
    """``SRSS`` — runtime stream stats.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      4    u32    Bitrate
    4      2    u16    Cache used
    6      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'SRSS'
    PRETTY = 'streaming-stats'

    bitrate = u32(at=0)
    cache   = u16(at=4)

    def __repr__(self):
        return f'<streaming-stats bitrate={self.bitrate} cache={self.cache}>'


# -----------------------------------------------------------------------------
# Restored upstream commands (0.15, 2026-09-11)
#
# These Send classes existed in upstream pyatem and were dropped from the
# fork because nothing exercised them. They are back, declared in the DSL
# with the exact byte layout of upstream's struct.pack strings and pinned
# byte-for-byte in tests/test_restored_upstream_commands.py. They have NOT
# been re-verified against a switcher in this fork; treat them as upstream
# did, and Wireshark-check before relying on a write on your model.
# -----------------------------------------------------------------------------


class StreamingServiceSetCommand(Send):
    """``CRSS`` — live-stream target settings (Live Stream settings of the
    Output menu).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 name, 1 url, 2 key, 3 min+max bitrate)
    1      64   str    Service name
    65     512  str    URL
    577    512  str    Stream key
    1089   3    ?      padding
    1092   4    u32    Minimum bitrate (bps)
    1096   4    u32    Maximum bitrate (bps)
    ====== ==== ====== ===========

    The two bitrates share one mask bit and must be given together.
    """
    CODE = 'CRSS'
    SIZE = 1100
    MASK_AT = 0

    name        = string(at=1, size=64, mask_bit=0)
    url         = string(at=65, size=512, mask_bit=1)
    key         = string(at=577, size=512, mask_bit=2)
    bitrate_min = u32   (at=1092, mask_bit=3)
    bitrate_max = u32   (at=1096, mask_bit=3)

    def __init__(self, name=None, url=None, key=None, bitrate_min=None, bitrate_max=None):
        if (bitrate_min is None) != (bitrate_max is None):
            raise ValueError("bitrate_min and bitrate_max must be given together")
        super().__init__(name=name, url=url, key=key,
                         bitrate_min=bitrate_min, bitrate_max=bitrate_max)


class StreamingAudioBitrateCommand(Send):
    """``STAB`` — audio bitrate for stream and recording (in practice always
    128k / 128k).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      4    u32    Minimum bitrate (bps)
    4      4    u32    Maximum bitrate (bps)
    ====== ==== ====== ===========
    """
    CODE = 'STAB'
    SIZE = 8

    bitrate_min = u32(at=0)
    bitrate_max = u32(at=4)

    def __init__(self, bitrate_min, bitrate_max):
        super().__init__(bitrate_min=bitrate_min, bitrate_max=bitrate_max)


class StreamingStatusSetCommand(Send):
    """``StrR`` — start or stop the live stream (ON AIR in the Output menu).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Streaming
    1      3    ?      unknown
    ====== ==== ====== ===========
    """
    CODE = 'StrR'
    SIZE = 4

    streaming = boolean(at=0)

    def __init__(self, streaming):
        super().__init__(streaming=streaming)


def set_streaming_service(conn, name=None, url=None, key=None, bitrate_min=None, bitrate_max=None):
    conn.send(StreamingServiceSetCommand(name=name, url=url, key=key,
                                         bitrate_min=bitrate_min, bitrate_max=bitrate_max))


def set_streaming_audio_bitrate(conn, bitrate_min, bitrate_max):
    conn.send(StreamingAudioBitrateCommand(int(bitrate_min), int(bitrate_max)))


def start_streaming(conn):
    conn.send(StreamingStatusSetCommand(True))


def stop_streaming(conn):
    conn.send(StreamingStatusSetCommand(False))
