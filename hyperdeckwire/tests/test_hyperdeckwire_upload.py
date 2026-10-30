# SPDX-License-Identifier: MIT
"""Unit tests for hyperdeckwire.upload.

Hyperdeck FTP behaves a little oddly compared to a stock FTP server:
storage slots are top-level numeric directories (``/1/``, ``/2/``,
``/3/``), STOR at the FTP root is rejected with 550, and the unit
expects you to CWD into the slot dir before uploading. The upload
helper auto-detects the slot dir to spare callers from having to know
this.

These tests don't need a real Hyperdeck — they monkeypatch
``ftplib.FTP`` with a fake that records the call sequence so we can
assert on the wire-level behaviour (login -> nlst -> cwd -> storbinary).
"""

import os

import pytest

import hyperdeckwire.upload as upload_module
from hyperdeckwire.upload import UploadCancelled, UploadResult, upload_clip


class _FakeFTP:
    """In-memory FTP stand-in that records every operation."""

    # Test knobs: how many pieces storbinary feeds the callback, and whether
    # DELE removes the file. Class attributes, since the fake is built inside
    # the call under test (patch them on the class).
    chunks = 2
    delete_works = True

    def __init__(self, host, timeout=None):
        self.host = host
        self.timeout = timeout
        self.calls: list = []
        self.cwd_stack: list = []
        # Test-controlled state, set by the test before the call:
        self.root_entries = ['1', 'System Volume Information', '.Trashes']
        self.login_should_fail = False
        self.stor_should_fail = False
        self.slot_files = ['existing.mp4']
        self.chunks_sent = 0

    # context-manager surface

    def __enter__(self):
        self.calls.append(('connect', self.host))
        return self

    def __exit__(self, *exc):
        self.calls.append(('close',))

    # ftplib API surface used by upload_clip

    def login(self, user='', passwd=''):
        self.calls.append(('login', user, passwd))
        if self.login_should_fail and user == '' and passwd == '':
            import ftplib
            raise ftplib.error_perm('530 anonymous required')

    def nlst(self, *args):
        self.calls.append(('nlst',) + tuple(args))
        if self.cwd_stack:
            return list(self.slot_files)
        return self.root_entries

    def cwd(self, path):
        self.calls.append(('cwd', path))
        self.cwd_stack.append(path)

    def storbinary(self, cmd, fp, callback=None):
        data = fp.read()
        self.calls.append(('storbinary', cmd, len(data)))
        self.slot_files.append(cmd.split(' ', 1)[1])   # the deck creates the file at once
        # Feed the callback the data in ``self.chunks`` pieces so tests can
        # verify it gets driven during a real upload (and stop it part-way).
        if callback is not None:
            step = max(1, len(data) // self.chunks)
            for i in range(0, len(data), step):
                callback(data[i:i + step])
                self.chunks_sent += 1
        if self.stor_should_fail:
            import ftplib
            raise ftplib.error_perm('550 file unavailable')

    sock = None

    def getresp(self):
        self.calls.append(('getresp',))
        import ftplib
        raise ftplib.error_temp('426 Connection closed; transfer aborted.')

    def delete(self, name):
        self.calls.append(('delete', name))
        if self.delete_works and name in self.slot_files:
            self.slot_files.remove(name)


@pytest.fixture
def fake_ftp(monkeypatch):
    """Patch hyperdeckwire.upload.ftplib.FTP to construct a _FakeFTP."""
    instances = []

    def _factory(host, timeout=None):
        inst = _FakeFTP(host, timeout=timeout)
        instances.append(inst)
        return inst

    monkeypatch.setattr(upload_module.ftplib, 'FTP', _factory)
    return instances


@pytest.fixture
def tmp_clip(tmp_path):
    """Write a small file we can pretend is a video clip."""
    path = tmp_path / 'sample clip.mp4'
    path.write_bytes(b'\x00\x01\x02' * 1024)  # 3 KB
    return str(path)


def test_upload_clip_logs_in_anonymous_and_cwds_to_slot(fake_ftp, tmp_clip):
    result = upload_clip('192.0.2.11', tmp_clip)
    assert isinstance(result, UploadResult)
    assert result.name == 'sample clip.mp4'
    assert result.size == os.path.getsize(tmp_clip)
    assert result.slot_dir == '1'

    ftp = fake_ftp[0]
    # Login first, then list root, then CWD into the slot, then STOR.
    call_kinds = [c[0] for c in ftp.calls]
    assert call_kinds == ['connect', 'login', 'nlst', 'cwd', 'storbinary', 'close']
    assert ftp.calls[1] == ('login', '', '')
    assert ftp.calls[3] == ('cwd', '1')
    assert ftp.calls[4][0] == 'storbinary'
    assert ftp.calls[4][1] == 'STOR sample clip.mp4'


def test_upload_clip_explicit_slot_skips_auto_detect(fake_ftp, tmp_clip):
    upload_clip('192.0.2.11', tmp_clip, slot=2)
    ftp = fake_ftp[0]
    # No nlst — we knew which slot we wanted.
    call_kinds = [c[0] for c in ftp.calls]
    assert 'nlst' not in call_kinds
    assert ('cwd', '2') in ftp.calls


def test_upload_clip_falls_back_to_anonymous_user_on_login_perm(
        fake_ftp, tmp_clip, monkeypatch):
    """Some FTP servers reject the bare empty-user login but accept
    the canonical ``anonymous`` user. The helper retries that path."""
    # Monkeypatch the FakeFTP class to make the first login attempt
    # fail with error_perm and the second succeed.
    original_factory = upload_module.ftplib.FTP

    class _StrictFTP(_FakeFTP):
        def __init__(self, host, timeout=None):
            super().__init__(host, timeout=timeout)
            self.login_should_fail = True

        def login(self, user='', passwd=''):
            self.calls.append(('login', user, passwd))
            if user == '' and passwd == '':
                import ftplib
                raise ftplib.error_perm('530')
            # Anonymous-explicit succeeds.

    monkeypatch.setattr(upload_module.ftplib, 'FTP',
                        lambda host, timeout=None: _StrictFTP(host, timeout))
    upload_clip('192.0.2.11', tmp_clip)
    # No assertion on instances here; just confirming no exception leaked.


def test_upload_clip_raises_when_no_slot_dir_at_root(fake_ftp, tmp_clip):
    """If FTP root has no numeric subdir, the helper can't auto-detect
    and refuses honestly rather than uploading to root (which Hyperdeck
    rejects with 550)."""
    fake_ftp_factory_state = {'instance': None}

    def make_no_slots(host, timeout=None):
        inst = _FakeFTP(host, timeout=timeout)
        inst.root_entries = ['System Volume Information', '.Trashes']
        fake_ftp_factory_state['instance'] = inst
        return inst

    import hyperdeckwire.upload as up
    up.ftplib.FTP = make_no_slots  # restored by fake_ftp fixture teardown

    with pytest.raises(OSError, match='no slot dir'):
        upload_clip('192.0.2.11', tmp_clip)


def test_upload_clip_auto_detects_named_volume(fake_ftp, tmp_clip):
    """Models that name their FTP mounts (sd1/usb) instead of numbering
    them must still auto-detect. SD is preferred over USB."""
    def make_named(host, timeout=None):
        inst = _FakeFTP(host, timeout=timeout)
        inst.root_entries = ['usb', 'sd1', '.Trashes']
        fake_ftp.append(inst)
        return inst

    import hyperdeckwire.upload as up
    up.ftplib.FTP = make_named  # restored by fake_ftp fixture teardown

    result = upload_clip('192.0.2.12', tmp_clip)
    assert result.slot_dir == 'sd1'
    assert ('cwd', 'sd1') in fake_ftp[-1].calls


def test_upload_clip_named_volume_usb_only(fake_ftp, tmp_clip):
    """When only USB is present, use it."""
    def make_usb(host, timeout=None):
        inst = _FakeFTP(host, timeout=timeout)
        inst.root_entries = ['usb']
        fake_ftp.append(inst)
        return inst

    import hyperdeckwire.upload as up
    up.ftplib.FTP = make_usb

    result = upload_clip('192.0.2.12', tmp_clip)
    assert result.slot_dir == 'usb'


def test_upload_clip_ignores_system_dirs_when_no_volume(fake_ftp, tmp_clip):
    """System-only roots still refuse honestly — System Volume Information
    is not a storage volume."""
    def make_system_only(host, timeout=None):
        inst = _FakeFTP(host, timeout=timeout)
        inst.root_entries = ['System Volume Information', '.Trashes']
        fake_ftp.append(inst)
        return inst

    import hyperdeckwire.upload as up
    up.ftplib.FTP = make_system_only

    with pytest.raises(OSError, match='no slot dir'):
        upload_clip('192.0.2.12', tmp_clip)


def test_upload_clip_raises_file_not_found_for_missing_source(fake_ftp):
    with pytest.raises(FileNotFoundError):
        upload_clip('192.0.2.11', '/nonexistent/path/to/clip.mp4')


def test_upload_clip_invokes_progress_callback(fake_ftp, tmp_clip):
    progress: list = []
    upload_clip('192.0.2.11', tmp_clip,
                progress_callback=progress.append)
    # FakeFTP feeds callback two chunks — by the end the running total
    # equals the full file size.
    assert progress[-1] == os.path.getsize(tmp_clip)


def test_upload_result_throughput_calculation():
    r = UploadResult(name='x.mp4', size=50_000_000,
                     duration_seconds=2.0, slot_dir='1')
    # 50 MB / 2s = 25 MB/s
    assert r.throughput_mb_s == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# should_cancel: stopping a transfer part-way.
# ---------------------------------------------------------------------------

def test_cancel_stops_the_transfer_and_removes_the_partial_file(fake_ftp, tmp_clip, monkeypatch):
    monkeypatch.setattr(upload_module, 'CANCEL_POLL_SECONDS', 0)
    monkeypatch.setattr(_FakeFTP, 'chunks', 10)
    asked = []

    def should_cancel():
        asked.append(1)
        return len(asked) >= 3

    with pytest.raises(UploadCancelled) as info:
        upload_clip('10.0.0.1', tmp_clip, should_cancel=should_cancel)
    name = os.path.basename(tmp_clip)
    assert info.value.name == name
    assert info.value.partial_removed is True
    assert 0 < info.value.bytes_sent < os.path.getsize(tmp_clip)
    ftp = fake_ftp[0]
    assert ('delete', name) in ftp.calls
    assert name not in ftp.slot_files
    assert ftp.chunks_sent == 2            # the third chunk's check stopped it


def test_cancel_says_so_when_the_partial_file_stays(fake_ftp, tmp_clip, monkeypatch):
    monkeypatch.setattr(upload_module, 'CANCEL_POLL_SECONDS', 0)
    monkeypatch.setattr(_FakeFTP, 'delete_works', False)
    with pytest.raises(UploadCancelled, match='still on the deck') as info:
        upload_clip('10.0.0.1', tmp_clip, should_cancel=lambda: True)
    assert info.value.partial_removed is False


def test_should_cancel_is_asked_at_most_once_per_poll_interval(fake_ftp, tmp_clip, monkeypatch):
    monkeypatch.setattr(upload_module, 'CANCEL_POLL_SECONDS', 3600)
    monkeypatch.setattr(_FakeFTP, 'chunks', 10)
    asked = []
    result = upload_clip('10.0.0.1', tmp_clip, should_cancel=lambda: asked.append(1) or True)
    assert asked == []                     # a fast transfer is never interrupted
    assert result.name == os.path.basename(tmp_clip)


def test_a_should_cancel_that_raises_does_not_stop_the_upload(fake_ftp, tmp_clip, monkeypatch):
    monkeypatch.setattr(upload_module, 'CANCEL_POLL_SECONDS', 0)

    def broken():
        raise RuntimeError('database went away')

    result = upload_clip('10.0.0.1', tmp_clip, should_cancel=broken)
    assert result.name == os.path.basename(tmp_clip)
