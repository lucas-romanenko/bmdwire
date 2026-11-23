# SPDX-License-Identifier: LGPL-3.0-only
"""Closing a session ends its UDP thread without an error line.

Every close says goodbye (the state goes to CLOSED) and then closes the
socket, which the UDP thread, waiting in select(), then meets: EBADF on
POSIX, WSAENOTSOCK (10038) on Windows. It was logged as an ERROR, "UDP
thread exiting", once for every session WebATEM's uploader closed and every
session ``python -m atemwire.sessions`` counted, on stderr.
"""
import logging

from atemwire import sessions
from atemwire.tests.test_sessions import TableSwitcher
from atemwire.transport import UdpProtocol


def _run_and_close(state):
    t = UdpProtocol('127.0.0.1')
    t.thread.start()
    t.state = state
    t.sock.close()
    t.thread_queue.put(None)                    # wake select() now
    t.thread.join(2.0)
    assert not t.thread.is_alive()
    t.thread_queue.close()


def test_a_session_closed_on_purpose_ends_quietly(caplog):
    with caplog.at_level(logging.DEBUG, logger='UdpTransport'):
        _run_and_close(UdpProtocol.STATE_CLOSED)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any('closed socket' in r.getMessage() for r in caplog.records)


def test_a_socket_closed_under_a_live_session_is_still_an_error(caplog):
    with caplog.at_level(logging.DEBUG, logger='UdpTransport'):
        _run_and_close(UdpProtocol.STATE_ESTABLISHED)
    assert any('UDP thread exiting' in r.getMessage() for r in caplog.records
               if r.levelno >= logging.ERROR)


def test_windows_closed_socket_counts_too():
    t = UdpProtocol('127.0.0.1')
    try:
        assert t._closed_socket(OSError(10038, 'An operation was attempted on something that is not a socket'))
        assert not t._closed_socket(OSError(101, 'Network is unreachable'))
    finally:
        t.sock.close()
        t.thread_queue.close()


def test_the_sessions_command_prints_only_its_result(capsys, monkeypatch):
    # No handler is configured, so Python's last resort printed every
    # transport ERROR on stderr.
    monkeypatch.setattr(logging.getLogger('UdpTransport'), 'level', logging.NOTSET)
    with TableSwitcher(slots=3) as sw:
        assert sessions.main([sw.ip]) == 0
    out, err = capsys.readouterr()
    assert 'free     3' in out
    assert 'UDP thread' not in err and 'Timeout reading' not in err
    assert err.startswith(f'{sw.ip}: counting free sessions.')
