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
