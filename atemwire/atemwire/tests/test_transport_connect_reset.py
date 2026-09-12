# SPDX-License-Identifier: LGPL-3.0-only
"""connect() must wipe the OLD session's receive bookkeeping.

A pooled/reconnecting transport reuses one ``UdpProtocol`` object across
sessions. ``received_packets`` drives BOTH the retransmission dup-drop and the
contiguous-ACK high-water walk; if a reconnect leaves stale sequence numbers in
it, the new session can falsely ACK a gap it never received (and drop the
ATEM's retransmission as a "duplicate") — silent reliable-data loss until the
next full reconnect. connect() must clear it and the ``had_traffic`` flag along
with the other per-session state it already resets.
"""
import types

from atemwire.transport import UdpProtocol


def test_connect_clears_stale_receive_state(monkeypatch):
    p = UdpProtocol('127.0.0.1')
    try:
        # Simulate an in-place reconnect: the object still carries the OLD
        # session's receive bookkeeping.
        p.received_packets.append(1234)
        p.received_packets.append(1235)
        p.had_traffic = True
        p.state = UdpProtocol.STATE_CLOSED  # connect() requires CLOSED

        # Isolate from the wire: skip the real UDP thread and the SYN send.
        p.thread = types.SimpleNamespace(is_alive=lambda: True)
        sent = []
        monkeypatch.setattr(p, '_send_packet', lambda pkt: sent.append(pkt))

        p.connect()

        assert len(p.received_packets) == 0        # stale seqs gone
        assert p.had_traffic is False              # traffic flag reset
        assert p.state == UdpProtocol.STATE_SYN_SENT
        assert len(sent) == 1                       # a fresh SYN went out
    finally:
        p.sock.close()
