# SPDX-License-Identifier: LGPL-3.0-only
"""A UDP thread that has ended never strands whoever pumps the protocol.

The thread puts one None (end of stream) on the receive queue as it ends,
and the first loop() takes it; every loop() after that waited on the queue
for good, so a caller's own deadline never came round (a bench whose code
ended the thread mid-upload, on macOS, 2026-11-24: 20 minutes until Ctrl-C).
"""
import threading
import time

from atemwire.protocol import AtemProtocol
from atemwire.transport import UdpProtocol


def a_protocol_whose_udp_thread_died():
    p = AtemProtocol('127.0.0.1', 1, aggressive_drain=True)
    tr = p.transport
    tr.state = UdpProtocol.STATE_ESTABLISHED
    tr.had_traffic = True

    def boom():
        raise RuntimeError('something unexpected in the UDP thread')
    tr._udp_thread_loop = boom
    tr.thread = threading.Thread(target=tr._udp_thread, daemon=True)
    tr.thread.start()
    tr.thread.join(2)
    assert not tr.thread.is_alive()
    return p


def test_loop_returns_again_and_again_once_the_thread_is_gone():
    p = a_protocol_whose_udp_thread_died()
    t0 = time.monotonic()
    p.loop()                                     # takes the thread's None
    returned = []

    def pump():
        for _ in range(3):
            p.loop()
            returned.append(time.monotonic())
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    th.join(5)
    assert len(returned) == 3, 'loop() blocked for good after the end-of-stream marker'
    assert returned[-1] - t0 < 3 * (UdpProtocol.DEAD_THREAD_CHECK + UdpProtocol.DEAD_THREAD_PAUSE) + 1
    p.transport.sock.close()


def test_a_caller_deadline_holds_with_the_thread_gone():
    p = a_protocol_whose_udp_thread_died()
    end = time.monotonic() + 1.0
    done = threading.Event()

    def pump_until_deadline():
        while time.monotonic() < end:
            p.loop()
        done.set()
    threading.Thread(target=pump_until_deadline, daemon=True).start()
    assert done.wait(3), 'the deadline never came round'
    p.transport.sock.close()
