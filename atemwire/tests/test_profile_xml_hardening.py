# SPDX-License-Identifier: LGPL-3.0-only
"""F7 (U1): ``Profile.from_xml`` rejects DTD / <!ENTITY> XML before parsing,
closing the entity-expansion ("billion laughs") DoS. stdlib ElementTree
expands internal entities with no guard, so an attack needs a DOCTYPE/DTD —
which real ATEM profile XML never contains.
"""
import pytest

from atemwire import Profile

_MINIMAL = '<Profile majorVersion="2" minorVersion="1"/>'

# Classic billion-laughs: nested internal entities. ElementTree would expand
# these; the guard must reject the input before ET.fromstring runs.
_BILLION_LAUGHS = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE lolz ['
    '<!ENTITY lol "lol">'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
    ']>'
    '<Profile majorVersion="2" minorVersion="1">&lol3;</Profile>'
)


def test_parses_minimal_profile():
    assert Profile.from_xml(_MINIMAL) is not None


def test_accepts_bytes_input():
    assert Profile.from_xml(_MINIMAL.encode('utf-8')) is not None


def test_rejects_billion_laughs():
    with pytest.raises(ValueError) as exc:
        Profile.from_xml(_BILLION_LAUGHS)
    msg = str(exc.value).lower()
    assert 'entity' in msg or 'doctype' in msg


def test_rejects_bare_doctype():
    with pytest.raises(ValueError):
        Profile.from_xml('<!DOCTYPE x>' + _MINIMAL)


def test_rejects_entity_declaration():
    with pytest.raises(ValueError):
        Profile.from_xml('<!DOCTYPE x [<!ENTITY e "AAAA">]>' + _MINIMAL)


def test_reject_is_case_insensitive():
    # XML keywords are uppercase, but over-rejecting a lowercased declaration
    # is harmless and safer than a parser that tolerates it.
    with pytest.raises(ValueError):
        Profile.from_xml('<!doctype x [<!entity e "z">]>' + _MINIMAL)


def test_prolog_comment_is_not_a_false_positive():
    # A leading comment starts '<!--', not '<!DOCTYPE'/'<!ENTITY' — must parse.
    assert Profile.from_xml('<!-- a note -->' + _MINIMAL) is not None


# --- bytes input + encoding sidesteps (from_file / content_change read bytes) ---

def test_rejects_billion_laughs_as_utf8_bytes():
    with pytest.raises(ValueError) as exc:
        Profile.from_xml(_BILLION_LAUGHS.encode('utf-8'))
    assert 'entity' in str(exc.value).lower() or 'doctype' in str(exc.value).lower()


def test_parses_minimal_profile_as_bytes():
    assert Profile.from_xml(_MINIMAL.encode('utf-8')) is not None


def test_rejects_utf16_le_encoded_dtd_no_bom():
    # THE wide-encoding sidestep: UTF-16-LE bytes decode (as utf-8) to a
    # NUL-laden str that hides the DTD from the text regex, but ET.fromstring
    # would re-detect UTF-16 and expand the entity. The NUL guard must reject
    # it BEFORE ET sees it. (Verified exploitable without the guard.)
    with pytest.raises(ValueError) as exc:
        Profile.from_xml(_BILLION_LAUGHS.encode('utf-16-le'))
    assert 'nul' in str(exc.value).lower() or 'utf-8' in str(exc.value).lower()


def test_rejects_utf16_be_encoded_dtd():
    with pytest.raises(ValueError):
        Profile.from_xml(_BILLION_LAUGHS.encode('utf-16-be'))


def test_rejects_utf16_bytes_with_bom():
    # UTF-16 with a BOM starts 0xff/0xfe → fails the utf-8 decode outright.
    with pytest.raises((ValueError, UnicodeDecodeError)):
        Profile.from_xml(_BILLION_LAUGHS.encode('utf-16'))


def test_rejects_str_declaring_utf16_encoding_with_dtd():
    # A str whose XML declaration claims a non-UTF-8 encoding: the guard runs
    # on the str regardless of the declared encoding, so the DTD is caught.
    xml = ('<?xml version="1.0" encoding="utf-16"?>'
           '<!DOCTYPE d [<!ENTITY a "AAAA">]>' + _MINIMAL)
    with pytest.raises(ValueError):
        Profile.from_xml(xml)


def test_rejects_bare_nul_in_str():
    with pytest.raises(ValueError):
        Profile.from_xml('<Profile\x00 majorVersion="2" minorVersion="1"/>')


def test_parses_non_ascii_input_label_str_and_bytes():
    # False-positive guard: a legitimate profile with accented content (an input
    # label like "Caméra") must parse. The NUL/DTD
    # guards reject only the attack shapes — non-ASCII UTF-8 has no NUL and no
    # DTD, so it must pass as BOTH str and utf-8 bytes.
    xml = ('<Profile majorVersion="2" minorVersion="1">'
           '<Settings><Input long="Caméra Café" short="CAMé"/></Settings>'
           '</Profile>')
    assert Profile.from_xml(xml) is not None
    assert Profile.from_xml(xml.encode('utf-8')) is not None
