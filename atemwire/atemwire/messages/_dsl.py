# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Declarative wire-format framework for ATEM messages.

Each ATEM packet has a 4-char code (``CutP``, ``PrgI``, ``ColV`` etc.) and a
fixed-or-variable byte payload. ``Send`` and ``Recv`` are the base classes
for the two directions; subclasses declare layout via class-level ``Field``
descriptors and the framework handles encoding / decoding.

Send example::

    class ColorGeneratorCommand(Send):
        CODE = 'CClV'
        SIZE = 8
        MASK_AT = 0

        index       = u8 (at=1)
        hue         = u16(at=2, mask_bit=0, scale=10)
        saturation  = u16(at=4, mask_bit=1, scale=1000)
        luma        = u16(at=6, mask_bit=2, scale=1000)

        def __init__(self, index, hue=None, saturation=None, luma=None):
            super().__init__(index=index, hue=hue,
                             saturation=saturation, luma=luma)

The instance behaves like a hand-written ``Command`` — ``get_command()``
returns the full packet bytes (8-byte header + payload), and the existing
``ATEMConnection.send(cmd)`` consumes it unchanged.

Recv example::

    class ColorGeneratorField(Recv):
        CODE = 'ColV'

        index       = u8 (at=0)
        hue         = u16(at=2, scale=10)
        saturation  = u16(at=4, scale=1000)
        luma        = u16(at=6, scale=1000)

Recv subclasses get a ``__init__(raw_bytes)`` for free; declared fields are
unpacked at their offsets and exposed as instance attributes.

Field options:
- ``at``        — byte offset into the payload
- ``mask_bit``  — bit index in the mask byte that gates this field on Send
                  (ignored on Recv). When set, the field is optional;
                  unset values aren't written and the mask byte is OR'd
                  with ``1 << mask_bit``.
- ``scale``     — divisor on Recv (wire → display), multiplier on Send
                  (display → wire). e.g. ``scale=10`` means display value
                  ``180.0`` packs as wire ``1800`` and unpacks back to
                  ``180.0``. Default 1 (no scaling).
- ``tail(pad_to=N)`` — one variable-length bytes field appended after
                  ``SIZE`` (declare it last); ``string`` takes ``mask_bit``
                  like the numeric fields. Both added in 0.15.

The docstring offset tables on every Send / Recv class are checked against
these declarations by ``tests/test_docstring_tables_match_declarations.py``.
"""

import struct
from typing import Optional


class Field:
    """Base for field descriptors. Holds wire layout + display↔wire transforms.

    Subclasses set ``fmt`` (struct format string) and ``size`` (width in bytes).
    Override ``pack`` / ``unpack`` for non-numeric conversions (booleans,
    strings, enums, etc.)."""

    fmt: str = ''
    size: int = 0

    def __init__(self, at: int, mask_bit: Optional[int] = None,
                 scale: float = 1.0):
        self.at = at
        self.mask_bit = mask_bit
        self.scale = scale

    def pack(self, buf: bytearray, value) -> None:
        """Encode ``value`` into ``buf`` at ``self.at``. Default: scale and
        struct-pack as the field's primitive type.

        Scaling rounds to the nearest integer rather than truncating
        toward zero — important for float inputs where ``1.499 * 100``
        should land at wire ``150``, not ``149``. Matches the
        ``int(round(value * scale))`` pattern the hand-rolled callers
        in operations.py and messages/fairlight.py use."""
        if self.scale != 1.0:
            value = int(round(value * self.scale))
        struct.pack_into(self.fmt, buf, self.at, value)

    def unpack(self, raw: bytes):
        """Decode the field's bytes from ``raw``. Default: struct-unpack and
        apply inverse scale (wire / scale → display)."""
        wire = struct.unpack_from(self.fmt, raw, self.at)[0]
        return wire / self.scale if self.scale != 1.0 else wire


class u8(Field):
    fmt = '>B'
    size = 1


class u16(Field):
    fmt = '>H'
    size = 2


class u32(Field):
    fmt = '>I'
    size = 4


class i8(Field):
    fmt = '>b'
    size = 1


class i16(Field):
    fmt = '>h'
    size = 2


class i32(Field):
    fmt = '>i'
    size = 4


class boolean(Field):
    """1-byte boolean. Wire value 0 → False, anything else → True.
    Pack writes 1 for truthy, 0 for falsy."""
    fmt = '>B'
    size = 1

    def pack(self, buf: bytearray, value) -> None:
        struct.pack_into('>B', buf, self.at, 1 if value else 0)

    def unpack(self, raw: bytes) -> bool:
        return struct.unpack_from('>B', raw, self.at)[0] != 0


class string(Field):
    """Fixed-length NUL-terminated UTF-8 string. ``size`` is the number of
    raw bytes occupied by the field; on unpack, anything from the first
    NUL byte onward is dropped. Takes ``mask_bit`` like the numeric fields
    (added 0.15 so CRMS / CRSS need no hand-written mask handling)."""

    def __init__(self, at: int, size: int, mask_bit: Optional[int] = None):
        super().__init__(at, mask_bit=mask_bit)
        self.size = size
        self.fmt = f'>{size}s'

    def pack(self, buf: bytearray, value) -> None:
        encoded = value.encode('utf-8') if isinstance(value, str) else bytes(value)
        struct.pack_into(self.fmt, buf, self.at, encoded[:self.size])

    def unpack(self, raw: bytes) -> str:
        raw_bytes = struct.unpack_from(self.fmt, raw, self.at)[0]
        return raw_bytes.split(b'\x00')[0].decode('utf-8', errors='replace')


class tail(Field):
    """Variable-length bytes appended after the fixed ``SIZE`` bytes of the
    payload (0.15). Declare it last. The value is ``bytes``; ``pad_to``
    zero-pads a shorter value up to that many bytes, which is how the
    camera-control command pads its data block to 8. On Recv the field
    unpacks as everything from ``SIZE`` to the end of the packet.

    ``at`` is filled in by the metaclass from the class's ``SIZE``, so a
    tail always starts exactly where the fixed layout ends."""
    size = 0

    def __init__(self, pad_to: Optional[int] = None):
        super().__init__(at=-1)
        self.pad_to = pad_to

    def pack(self, buf: bytearray, value) -> None:
        data = bytes(value)
        if self.pad_to and len(data) < self.pad_to:
            data += b'\0' * (self.pad_to - len(data))
        buf.extend(data)

    def unpack(self, raw: bytes) -> bytes:
        return bytes(raw[self.at:])


class _MessageMeta(type):
    """Walks Field-typed class attributes and registers them in a class-level
    ``_fields`` dict (declaration order, Python 3.7+).

    Removes the Field class attributes from the class dict after registry —
    otherwise ``getattr(instance, fname, None)`` would fall back to the class
    attribute (the Field descriptor) for fields that the caller didn't pass
    in kwargs, and the ``if value is None: continue`` gate in ``get_command``
    would never fire. Removing them makes unset fields actually return None."""

    def __new__(mcs, name, bases, attrs):
        fields = {}
        for base in bases:
            fields.update(getattr(base, '_fields', {}))
        size = attrs.get('SIZE')
        tails = 0
        for k, v in list(attrs.items()):
            if isinstance(v, Field):
                if isinstance(v, tail):
                    tails += 1
                    if size is None:
                        raise TypeError(f'{name}: a tail field needs SIZE on the class')
                    v.at = size
                fields[k] = v
                del attrs[k]
        if tails > 1:
            raise TypeError(f'{name}: at most one tail field')
        attrs['_fields'] = fields
        cls = super().__new__(mcs, name, bases, attrs)
        if tails and list(fields.values())[-1] is not [v for v in fields.values() if isinstance(v, tail)][0]:
            raise TypeError(f'{name}: the tail field must be declared last')
        return cls


class _Message(metaclass=_MessageMeta):
    """Common base for Send and Recv. Holds the 4-char wire code + size."""

    CODE: str = ''   # 4-char ATEM packet code; subclass sets
    SIZE: int = 0    # total payload bytes (excluding 8-byte header)
    PRETTY: str = '' # mixerstate dict key (Recv subclasses set this)
    KEY_FORMAT = None  # struct.Struct for index extraction; None = top-level
                       # entry. Used by protocol.py to slice the index out of
                       # the raw payload before storing under PRETTY.


class Send(_Message):
    """Outgoing message. ``get_command()`` returns the full packet bytes
    (8-byte header + payload), ready for ``ATEMConnection.send``.

    Subclasses set ``CODE``, ``SIZE``, and optionally ``MASK_AT`` (the
    offset of the leading mask) and ``MASK_TYPE`` (``u8`` by default;
    set to ``u16`` or ``u32`` when more than 8 mask bits are needed —
    the wipe / DVE / advanced-chroma / DVE-fly packets use this)."""

    MASK_AT: Optional[int] = None
    MASK_TYPE = u8

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def get_command(self) -> bytes:
        buf = bytearray(self.SIZE)
        mask = 0
        for fname, f in self._fields.items():
            value = getattr(self, fname, None)
            if value is None:
                continue
            if f.mask_bit is not None:
                mask |= (1 << f.mask_bit)
            f.pack(buf, value)
        if self.MASK_AT is not None:
            struct.pack_into(self.MASK_TYPE.fmt, buf, self.MASK_AT, mask)
        header = struct.pack('>H 2x 4s', len(buf) + 8, self.CODE.encode())
        return header + bytes(buf)


class Recv(_Message):
    """Incoming message. ``__init__(raw)`` decodes the payload bytes
    into instance attributes for each declared field.

    Consumed by ``atemwire.protocol.AtemProtocol.save_field_data`` to populate
    mixerstate from incoming packets."""

    def __init__(self, raw: bytes):
        self.raw = raw
        for fname, f in self._fields.items():
            setattr(self, fname, f.unpack(raw))
