"""The bench scripts here need a live device, so the suite never runs them
against one. They are exercised nowhere else, so it checks:

  * every one parses;
  * every one that opens an ATEM session installs ``_exit_signals`` (a
    signal unwinds into the goodbye instead of abandoning the session on
    the switcher's small session table), and a bare-protocol one stopped
    mid-connect really says goodbye, in a child process with a real
    SIGTERM and a fake switcher;
  * L18 (2026-07-06): the delete-clip tool's FTP connect closes the
    connection when the anonymous login fallback fails too.
"""
import ast
import ftplib
import importlib.util
import os
import signal
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TOOLS = sorted(p for p in HERE.glob('*.py') if not p.name.startswith(('test_', '_')))


@pytest.mark.parametrize('path', TOOLS, ids=[p.name for p in TOOLS])
def test_bench_script_parses(path):
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _opens_an_atem_session(source):
    return any(name in source for name in ('AtemProtocol(', 'acquire_connection(', 'ATEM('))


def _main_block_installs(tree):
    for node in tree.body:
        if isinstance(node, ast.If) and '__main__' in ast.unparse(node.test):
            return '_exit_signals.install()' in ast.unparse(node)
    return False


def test_every_tool_that_opens_an_atem_session_installs_the_handler():
    checked = []
    for path in TOOLS:
        source = path.read_text(encoding='utf-8')
        if not _opens_an_atem_session(source):
            continue
        checked.append(path.name)
        assert _main_block_installs(ast.parse(source)), f'{path.name}: no _exit_signals.install()'
    assert len(checked) >= 7                 # the list really was walked


# Replaces the switcher for a real tool run in a child process: the session
# "connects", then the handshake hangs (as on a slow or full switcher) until
# the signal comes. The goodbye prints.
TOOL_CHILD = textwrap.dedent('''
    import runpy, sys, time, types
    sys.path[:0] = [{tools!r}, {repo!r}]
    import atemwire.protocol, atemwire.ready

    class Transport:
        sock = types.SimpleNamespace(close=lambda: None)
        def close_session(self):
            print('GOODBYE', flush=True)

    class Protocol:
        def __init__(self, ip=None, **kw):
            self.transport = Transport()
            self.mixerstate = {{}}
        def connect(self):
            pass
        def loop(self):
            time.sleep(0.01)

    def hanging_handshake(*a, **kw):
        print('READY', flush=True)
        time.sleep(30)

    atemwire.protocol.AtemProtocol = Protocol
    atemwire.ready.wait_ready = hanging_handshake
    sys.argv = [{tool!r}] + {args!r}
    runpy.run_path({tool!r}, run_name='__main__')
''')


@pytest.mark.skipif(not hasattr(signal, 'SIGHUP'), reason='POSIX signals')
@pytest.mark.parametrize('tool', ['atem_state_check.py', 'wipe_pattern_probe.py'])
def test_a_bare_tool_stopped_mid_connect_says_goodbye(tool):
    code = TOOL_CHILD.format(tools=str(HERE), repo=str(HERE.parent),
                             tool=str(HERE / tool), args=['192.0.2.10'])
    p = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, cwd=str(HERE.parent),
                         env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    while True:
        line = p.stdout.readline()
        assert line, p.communicate(timeout=20)[1]
        if line.strip() == 'READY':
            break
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=20)
    assert 'GOODBYE' in out, err
    assert p.returncode == 128 + signal.SIGTERM


def _load_delete_clip_tool():
    """tools/ is not a package: load the script by path."""
    path = HERE / 'hyperdeck_delete_clip.py'
    spec = importlib.util.spec_from_file_location('hyperdeck_delete_clip_tool', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_connect_ftp_closes_ftp_when_both_logins_fail(monkeypatch):
    tool = _load_delete_clip_tool()
    created = []

    class _FakeFTP:
        def __init__(self, host, timeout=None):
            self.closed = False
            self.logins = []
            created.append(self)

        def login(self, user, passwd):
            self.logins.append((user, passwd))
            raise ftplib.error_perm('530 Login incorrect.')

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        tool, 'ftplib',
        types.SimpleNamespace(FTP=_FakeFTP, error_perm=ftplib.error_perm),
    )
    with pytest.raises(ftplib.error_perm):
        tool._connect_ftp('192.0.2.20', 5.0)
    assert len(created) == 1
    assert created[0].logins == [('', ''), ('anonymous', '')]
    assert created[0].closed  # pre-fix: leaked to GC


def test_connect_ftp_fallback_login_success_keeps_connection_open(monkeypatch):
    tool = _load_delete_clip_tool()

    class _FakeFTP:
        def __init__(self, host, timeout=None):
            self.closed = False
            self.attempts = 0

        def login(self, user, passwd):
            self.attempts += 1
            if self.attempts == 1:
                raise ftplib.error_perm('530 Login incorrect.')

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        tool, 'ftplib',
        types.SimpleNamespace(FTP=_FakeFTP, error_perm=ftplib.error_perm),
    )
    ftp = tool._connect_ftp('192.0.2.20', 5.0)
    assert ftp.attempts == 2
    assert not ftp.closed
