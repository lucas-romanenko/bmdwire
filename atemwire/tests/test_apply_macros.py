# SPDX-License-Identifier: LGPL-3.0-only
"""Unit tests for ``atemwire.profile._apply_macros`` accounting under
the partial-upload policy.

Each macro is encoded **per-op**: ops the encoder doesn't recognise are
dropped from that macro's bytecode and noted in the result; the
remaining ops upload normally so the macro lands in its slot. The whole
macro only "fails" if the file-transfer itself errors. This matches
operator preference — half a macro that lands beats no macro at all,
as long as the dropped ops are clearly enumerated.

The actual file-transfer is monkey-patched out so these tests don't
need a live ATEM. The encoder runs for real — that's the part that
classifies an op as "no encoder for op id 'X'" when it can't be
mapped (e.g. Fairlight ops, which have no decoder either).
"""


from atemwire.profile import ApplyOptions, Profile


class _FakeProtocol:
    def __init__(self, connected: bool = True):
        self.connected = connected


class _FakeConn:
    """Minimal stand-in for ATEMConnection used by _apply_macros.

    ``_apply_macros`` checks ``conn.protocol.connected`` to decide
    whether to proceed; calls ``conn.send`` for the metadata-only
    (MSRc/MAct) fallback. The fake captures ``send`` calls so tests
    can also assert on the recording-fallback path.
    """
    def __init__(self, connected: bool = True):
        self.protocol = _FakeProtocol(connected=connected)
        self.mixerstate = {}
        self.sent = []

    def send(self, command):
        self.sent.append(command)


def _profile_with_macros(macro_xml_inner: str) -> Profile:
    """Build a minimal profile with the given inner XML for <MacroPool>."""
    xml = (
        '<Profile majorVersion="2" minorVersion="1">'
        '<MacroPool>'
        f'{macro_xml_inner}'
        '</MacroPool>'
        '</Profile>'
    )
    return Profile.from_xml(xml)


def _macros_only_opts() -> ApplyOptions:
    """Apply options that gate everything off except macros."""
    opts = ApplyOptions(restore_macros=True)
    for attr in dir(opts):
        if attr.startswith('restore_') and attr != 'restore_macros':
            cur = getattr(opts, attr)
            if isinstance(cur, bool):
                setattr(opts, attr, False)
            elif isinstance(cur, list) and cur and isinstance(cur[0], bool):
                setattr(opts, attr, [False] * len(cur))
    return opts


def _stub_upload_success(monkeypatch):
    """Replace upload_macro_bytecode with a no-op stub that records calls."""
    calls = []

    def _stub(protocol, slot, name, description, bytecode, *, timeout=10.0):
        calls.append({
            'slot': slot, 'name': name, 'description': description,
            'bytecode_len': len(bytecode),
        })

    monkeypatch.setattr('atemwire.macrotransfer.upload_macro_bytecode', _stub)
    return calls


# -----------------------------------------------------------------------------
# Clean uploads — every op encodes, no drops, no failures.
# -----------------------------------------------------------------------------

def test_apply_macros_two_clean_uploads_reports_2_of_2(monkeypatch):
    """Both macros encode every op cleanly + upload — report
    ``2/2 restored, 0 failed`` and route to ``applied``."""
    upload_calls = _stub_upload_success(monkeypatch)

    p = _profile_with_macros(
        '<Macro index="0" name="A" description="">'
        '  <Op id="ProgramInput" mixEffectBlockIndex="0" input="Camera1"/>'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '</Macro>'
        '<Macro index="1" name="B" description="">'
        '  <Op id="PreviewInput" mixEffectBlockIndex="0" input="Camera2"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())

    macro_lines = [a for a in result.applied if a.startswith('macros (')]
    assert len(macro_lines) == 1, f'expected one macro line, got {result.applied!r}'
    assert macro_lines[0] == 'macros (2/2 restored, 0 failed)'

    skipped_macro = [s for s in result.skipped if s.startswith('macros:')]
    assert not skipped_macro, f'should not be skipped: {skipped_macro!r}'

    # Both upload calls happened with the right slot indices.
    assert {c['slot'] for c in upload_calls} == {0, 1}


def test_apply_macros_single_clean_upload_reports_1_of_1(monkeypatch):
    """Single-macro pool with all-known ops reports ``1/1 restored``."""
    _stub_upload_success(monkeypatch)
    p = _profile_with_macros(
        '<Macro index="0" name="solo" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())
    macro_lines = [a for a in result.applied if a.startswith('macros (')]
    assert macro_lines == ['macros (1/1 restored, 0 failed)']


# -----------------------------------------------------------------------------
# Partial uploads — macro lands but some ops were dropped.
# -----------------------------------------------------------------------------

def test_apply_macros_partial_upload_reports_dropped_ops(monkeypatch):
    """Macro with a mix of known and unknown ops uploads with the known
    subset; the unknown ops are listed in the result message but the
    macro is still counted as restored (it's in its slot)."""
    upload_calls = _stub_upload_success(monkeypatch)

    p = _profile_with_macros(
        '<Macro index="0" name="WIDE CAM" description="">'
        '  <Op id="ProgramInput" mixEffectBlockIndex="0" input="Camera1"/>'
        '  <Op id="_TestSyntheticUnencodableOp2"'
        '      input="Camera1" sourceId="0" mixType="On"/>'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())

    macro_lines = [a for a in result.applied if a.startswith('macros (')]
    assert len(macro_lines) == 1
    line = macro_lines[0]
    # 1/1 restored (the macro DID land — partial upload counts as restored)
    assert '1/1 restored' in line, line
    # 1 macro had drops
    assert '1 with dropped ops' in line, line
    # The op id and slot should be enumerated
    assert "slot 0 ('WIDE CAM')" in line, line
    assert '_TestSyntheticUnencodableOp2' in line, line
    # Operator should see how many ops were dropped (singular here)
    assert 'dropped 1 op' in line, line

    # The macro WAS uploaded (not skipped) with the surviving ops.
    assert len(upload_calls) == 1
    # Bytecode should be 4-byte hdr + 4 params per op, two ops survived = 16 B.
    assert upload_calls[0]['bytecode_len'] == 16


def test_apply_macros_drops_with_two_unknown_ids_reported_distinctly(monkeypatch):
    """Each unique unknown op id appears in the drop list."""
    _stub_upload_success(monkeypatch)
    p = _profile_with_macros(
        '<Macro index="0" name="X" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '  <Op id="_TestSyntheticUnencodableOp2"'
        '      input="Camera1" sourceId="0" ratio="2"/>'
        '  <Op id="_TestSyntheticUnencodableOp"'
        '      input="Camera2" sourceId="0" threshold="-20"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())
    line = [a for a in result.applied if a.startswith('macros (')][0]
    assert 'dropped 2 ops' in line, line
    assert '_TestSyntheticUnencodableOp2' in line, line
    assert '_TestSyntheticUnencodableOp' in line, line


def test_apply_macros_drops_dedupes_repeated_op_ids(monkeypatch):
    """If the same unknown op id appears N times, it's listed once but
    the count says N."""
    _stub_upload_success(monkeypatch)
    p = _profile_with_macros(
        '<Macro index="0" name="A" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '  <Op id="_TestSyntheticUnencodableOp2"'
        '      input="Camera1" sourceId="0" mixType="On"/>'
        '  <Op id="_TestSyntheticUnencodableOp2"'
        '      input="Camera2" sourceId="0" mixType="Off"/>'
        '  <Op id="_TestSyntheticUnencodableOp2"'
        '      input="Camera3" sourceId="0" mixType="Off"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())
    line = [a for a in result.applied if a.startswith('macros (')][0]
    assert 'dropped 3 ops' in line, line
    # The id appears once in parens, not three times.
    assert line.count('_TestSyntheticUnencodableOp2') == 1, line


# -----------------------------------------------------------------------------
# All-dropped — every op fails the encoder, fall back to metadata-only.
# -----------------------------------------------------------------------------

def test_apply_macros_all_ops_dropped_uploads_metadata_only(monkeypatch):
    """If every op in a macro is unknown to the encoder, fall back to
    uploading empty bytecode via the same FTSD path so name +
    description still land WITHOUT triggering MSRc/MAct (which would
    light up Software Control's red record-border briefly). The macro
    is still counted as restored, and the result message flags
    "metadata only" so the operator knows it's an empty slot under
    the right name."""
    upload_calls = _stub_upload_success(monkeypatch)
    p = _profile_with_macros(
        '<Macro index="0" name="all-bad" description="all dropped">'
        '  <Op id="_TestSyntheticUnencodableOp2"'
        '      input="Camera1" sourceId="0" ratio="2"/>'
        '  <Op id="_TestSyntheticUnencodableOp"'
        '      input="Camera2" sourceId="0" threshold="-20"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())

    line = [a for a in result.applied if a.startswith('macros (')][0]
    assert '1/1 restored' in line, line
    assert '1 with dropped ops' in line, line
    assert 'dropped 2 ops' in line, line
    assert 'metadata only' in line, line

    # File-transfer upload WAS called — with empty bytecode.
    assert len(upload_calls) == 1
    call = upload_calls[0]
    assert call['slot'] == 0
    assert call['name'] == 'all-bad'
    assert call['description'] == 'all dropped'
    assert call['bytecode_len'] == 0
    # MSRc / MAct must NOT have been sent — those drive the red border
    # in ATEM Software Control. A Wireshark capture of BMD's "Restore
    # from State" shows it also avoids them for empty macros.
    from atemwire.messages import MacroRecordCommand, MacroActionCommand
    assert not any(isinstance(c, MacroRecordCommand) for c in conn.sent), (
        "MSRc must not be sent for empty macros (causes red record-border flash)")
    assert not any(isinstance(c, MacroActionCommand) for c in conn.sent), (
        "MAct must not be sent for empty macros")


def test_apply_macros_truly_empty_macro_no_drop_message(monkeypatch):
    """An <Macro/> with zero <Op> children also uses the empty-bytecode
    upload path (so name + description still land) but is NOT flagged
    as "with dropped ops" — there were no ops to drop in the first
    place. Same red-border concern as the all-dropped case."""
    upload_calls = _stub_upload_success(monkeypatch)
    p = _profile_with_macros(
        '<Macro index="7" name="meta-only" description="just a name"/>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())
    line = [a for a in result.applied if a.startswith('macros (')][0]
    assert line == 'macros (1/1 restored, 0 failed)'

    # Empty bytecode upload (no MSRc/MAct).
    assert len(upload_calls) == 1
    call = upload_calls[0]
    assert call['slot'] == 7
    assert call['name'] == 'meta-only'
    assert call['description'] == 'just a name'
    assert call['bytecode_len'] == 0
    from atemwire.messages import MacroRecordCommand, MacroActionCommand
    assert not any(isinstance(c, MacroRecordCommand) for c in conn.sent)
    assert not any(isinstance(c, MacroActionCommand) for c in conn.sent)


# -----------------------------------------------------------------------------
# Upload errors — the file-transfer itself fails.
# -----------------------------------------------------------------------------

def test_apply_macros_upload_error_reports_failed_with_message(monkeypatch):
    """An upload error (e.g. ATEM rejected, timeout) fails the macro
    with reason ``upload error: <message>``. The macro is NOT in
    "restored" — it didn't land."""
    def _failing_upload(protocol, slot, name, description, bytecode, *,
                        timeout=10.0):
        raise RuntimeError('ATEM rejected macro upload (status=not-found)')
    monkeypatch.setattr('atemwire.macrotransfer.upload_macro_bytecode',
                        _failing_upload)

    p = _profile_with_macros(
        '<Macro index="3" name="solo" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())

    skipped = [s for s in result.skipped if s.startswith('macros:')]
    assert len(skipped) == 1
    line = skipped[0]
    assert '0/1 restored, 1 failed' in line, line
    assert "slot 3 ('solo')" in line, line
    assert 'upload error' in line, line
    assert 'not-found' in line, line


def test_apply_macros_upload_error_distinct_from_drop(monkeypatch):
    """A macro with drops AND an upload error is counted as failed,
    not as restored-with-drops. Drops flag is per landed macro; failure
    is per macro that didn't land."""
    def _failing_upload(protocol, slot, name, description, bytecode, *,
                        timeout=10.0):
        raise RuntimeError('FTDE code 5')
    monkeypatch.setattr('atemwire.macrotransfer.upload_macro_bytecode',
                        _failing_upload)

    p = _profile_with_macros(
        '<Macro index="0" name="A" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '  <Op id="_TestSyntheticUnencodableOp"'
        '      input="Camera2" sourceId="0" threshold="-20"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())

    # Failed (upload error eclipses drops — macro didn't land).
    skipped = [s for s in result.skipped if s.startswith('macros:')]
    assert len(skipped) == 1
    line = skipped[0]
    assert '0/1 restored' in line, line
    assert '1 failed' in line, line
    # Should NOT also report "with dropped ops" for a failed macro.
    assert 'dropped' not in line, line


def test_apply_macros_mixed_success_drop_and_failure(monkeypatch):
    """A pool with one clean macro, one with-drops macro, and one
    upload-error macro produces a summary that accounts for all three."""
    fail_slots = {2}
    upload_calls = []

    def _selective_upload(protocol, slot, name, description, bytecode, *,
                          timeout=10.0):
        upload_calls.append(slot)
        if slot in fail_slots:
            raise RuntimeError('FTDE code 5')

    monkeypatch.setattr('atemwire.macrotransfer.upload_macro_bytecode',
                        _selective_upload)

    p = _profile_with_macros(
        '<Macro index="0" name="clean" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '</Macro>'
        '<Macro index="1" name="dropped" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '  <Op id="_TestSyntheticUnencodableOp"'
        '      input="Camera2" sourceId="0" threshold="-20"/>'
        '</Macro>'
        '<Macro index="2" name="boom" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '</Macro>'
    )
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())

    line = [a for a in result.applied if a.startswith('macros (')][0]
    assert '2/3 restored' in line, line
    assert '1 with dropped ops' in line, line
    assert '1 failed' in line, line
    # Order: drops section, then failures section.
    assert line.index('with dropped ops') < line.index('failed'), line


# -----------------------------------------------------------------------------
# Empty cases — no macros at all should produce no result line.
# -----------------------------------------------------------------------------

def test_apply_empty_macro_pool_emits_no_result_line(monkeypatch):
    """``<MacroPool/>`` with no `<Macro>` children: nothing happens, no
    line in either ``applied`` or ``skipped``."""
    _stub_upload_success(monkeypatch)
    p = _profile_with_macros('')
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())

    macro_applied = [a for a in result.applied if a.startswith('macros')]
    macro_skipped = [s for s in result.skipped if s.startswith('macros')]
    assert macro_applied == [], result.applied
    assert macro_skipped == [], result.skipped


def test_apply_no_macro_pool_element_emits_no_result_line(monkeypatch):
    """Profile with no `<MacroPool>` element at all is silent on macros."""
    _stub_upload_success(monkeypatch)
    xml = '<Profile majorVersion="2" minorVersion="1"></Profile>'
    p = Profile.from_xml(xml)
    conn = _FakeConn()
    result = p.apply(conn, _macros_only_opts())
    assert not [a for a in result.applied if a.startswith('macros')]
    assert not [s for s in result.skipped if s.startswith('macros')]


def test_apply_macros_no_protocol_routes_to_skipped(monkeypatch):
    """If the connection isn't ready yet, macros section is skipped
    with a clear reason — distinct from "fail" because we never tried
    the upload."""
    p = _profile_with_macros(
        '<Macro index="0" name="A" description="">'
        '  <Op id="Cut" mixEffectBlockIndex="0"/>'
        '</Macro>'
    )
    conn = _FakeConn(connected=False)
    result = p.apply(conn, _macros_only_opts())
    macro_applied = [a for a in result.applied if a.startswith('macros')]
    macro_skipped = [s for s in result.skipped if s.startswith('macros:')]
    assert macro_applied == []
    assert any('no connected protocol' in s for s in macro_skipped), \
        result.skipped
