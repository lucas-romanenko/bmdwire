# SPDX-License-Identifier: LGPL-3.0-only
"""A fake ATEM for the network matrix (``tools/netmatrix.py``), modelled on
captures of ATEM 1 M/E Constellation HD and 4K Plus units and on
atemwire's MEDIA_LOCK.md. What it does is what those showed; where a
capture never showed it, the choice is written down here.

Transport, per session
- hello answered at once; with every session taken (``max_sessions``) it
  answers "no room" (0x03);
- its reliable packets resent, every one not yet ACKed, every 80 ms (the
  captures: resends on the frame tick, 77-81 ms);
- our packets taken strictly in order: one out of order is dropped and
  never asked for again (330k packets of Constellation captures, no
  retransmission request), unless ``rtx_request`` (a Mini Pro asks);
- ACKed cumulatively every 16 packets or 10 ms;
- a reliable Time packet every 40 ms; a session silent for ``reap`` s
  is dropped.

State
- a dump at connect, packed into packets of up to 1,394 bytes of
  commands as the units send it (``dump_kb`` of it, the rest of the
  dump a model-specific padding command the client ignores), ending in
  InCm;
- CPgI / CPvI / CInL / CMvI answered to every session with PrgI / PrvI /
  InPr / MvIn, as a real unit does on any change.

Media
- store locks (PLCK / LOCK) granted in turn, LKST broadcast;
- uploads (FTSD): FTCD budgets of ``budget`` chunks of 1,396 bytes, data
  taken in order, FTDC to everyone with MPfe / MPrp once every byte and
  the FTFD are in; a macro upload makes its slot a nameless used macro at
  once (the #63 bench left "Macro 100" on a unit);
- downloads (FTSU): FTDa chunks of 1,396, at most 150 not yet FTUA'd;
- MAct 5 deletes a macro, CSTL clears a still.

Faults, from the control channel (stdin lines, see ``Control``):
``reboot S`` (silent S seconds, then up with every session, lock and
transfer gone and the media pool empty; macros kept), ``cut S`` / ``cut
0`` (the relay's cable), ``stats`` (one JSON line).
"""
import argparse
import hashlib
import heapq
import json
import os
import random
import select
import socket
import struct
import sys
import threading
import time

CHUNK = 1396
TICK = 0.04
RESEND_EVERY = 0.08
FTUA_WINDOW = 150
DGRAM_PAYLOAD = 1394


def cmd(name, body):
    return struct.pack('>H2x4s', len(body) + 8, name) + body


def mprp(index, used, name=b'', desc=b''):
    return cmd(b'MPrp', struct.pack('>H??HH', index, used, False, len(name), len(desc)) + name + desc)


def mpfe(slot, used, md5=bytes(16), name=b''):
    return cmd(b'MPfe', struct.pack('>BxH?16s2x', 0, slot, used, md5) + name + b'\0')


def inpr(index, name, short):
    return cmd(b'InPr', struct.pack('>H20s4s?xHHBxBB', index, name.encode()[:20],
                                    short.encode()[:4], False, 1, 1, 0, 0xff, 0x01))


def decoded_len(chunk):
    """Bytes a still's RLE chunk decodes to (8-byte words; a run is the
    0xFE x8 marker, a count, then the word)."""
    n, i, end = 0, 0, len(chunk)
    while i + 8 <= end:
        if chunk[i:i + 8] == b'\xfe' * 8 and i + 24 <= end:
            n += struct.unpack('>Q', chunk[i + 8:i + 16])[0] * 8
            i += 24
        else:
            n += 8
            i += 8
    return n


def rle_decode(data):
    out, i, end = bytearray(), 0, len(data)
    while i + 8 <= end:
        if data[i:i + 8] == b'\xfe' * 8 and i + 24 <= end:
            out += data[i + 16:i + 24] * struct.unpack('>Q', data[i + 8:i + 16])[0]
            i += 24
        else:
            out += data[i:i + 8]
            i += 8
    return bytes(out)


class Session:
    def __init__(self, addr):
        self.addr = addr
        self.sid = None
        self.seq = 0
        self.acked = 0
        self.sent = {}
        self.client_seq = None
        self.unacked_client = 0
        self.ack_timer = None
        self.next_resend = None
        self.last_heard = time.monotonic()
        self.rtx_asked = None
        self.rtx_at = 0.0


class Fake:
    def __init__(self, sock, *, max_sessions=5, dump_kb=200, budget=768, prep=(0.13, 0.4),
                 rtx_request=False, reap=5.0, inputs=20, seed=1, width=1920, height=1080):
        self.sock = sock
        self.max_sessions = max_sessions
        self.dump_kb = dump_kb
        self.budget = budget
        self.prep = prep
        self.rtx_request = rtx_request
        self.reap = reap
        self.width, self.height = width, height
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.timers = []
        self._tn = 0
        self.down_until = 0.0
        self.t0 = time.monotonic()
        self.stats = {'sessions': 0, 'refused': 0, 'reaped': 0, 'goodbyes': 0, 'reboots': 0,
                      'client_dropped_ooo': 0, 'client_dup': 0, 'resent': 0, 'rtx_requests': 0,
                      'uploads': [], 'downloads': 0, 'ftde': {}, 'commands': {}}
        self.inputs = inputs
        self.macros = {0: (b'Macro one', bytes(range(232)))}     # slot -> (name, bytecode)
        self.stills = {}                                         # slot -> (md5, rle bytes)
        self._power_on()

    # --- power ---
    def _power_on(self):
        self.sessions = {}
        self.next_sid = 0x8001 + self.rng.randrange(0x100)
        self.holder = {}             # store -> session
        self.waiters = {}            # store -> [session]
        self.lkst_pending = []
        self.transfer = None
        self.program = 1
        self.preview = 2
        self.labels = {i: ('Camera %d' % i, 'CM%d' % i) for i in range(1, self.inputs + 1)}
        self.mv = {w: w for w in range(16)}
        self.next_tick = time.monotonic() + TICK

    def reboot(self, seconds):
        self.stats['reboots'] += 1
        self.down_until = time.monotonic() + seconds
        self.stills = {}             # the media pool is RAM
        self._power_on()

    # --- transport ---
    def raw_send(self, s, raw):
        try:
            self.sock.sendto(raw, s.addr)
        except OSError:
            pass

    def send_reliable(self, s, payload):
        s.seq = (s.seq + 1) & 0x7fff
        raw = struct.pack('>HHHHHH', (0x01 << 11) | (12 + len(payload)),
                          s.sid, 0, 0, 0, s.seq) + payload
        s.sent[s.seq] = raw
        self.raw_send(s, raw)
        if s.next_resend is None:
            s.next_resend = time.monotonic() + RESEND_EVERY

    def send_ack(self, s):
        self.raw_send(s, struct.pack('>HHHHHH', (0x10 << 11) | 12, s.sid, s.client_seq or 0, 0, 0, 0))
        s.unacked_client = 0
        s.ack_timer = None

    def on_ack(self, s, n):
        moved = False
        while s.sent and ((n - s.acked) & 0x7fff) < 0x4000 and s.acked != n:
            s.acked = (s.acked + 1) & 0x7fff
            s.sent.pop(s.acked, None)
            moved = True
        if not s.sent:
            s.next_resend = None
        elif moved:
            s.next_resend = time.monotonic() + RESEND_EVERY

    def resend(self, s):
        for q in sorted(s.sent, key=lambda q: (q - s.acked) & 0x7fff):
            raw = bytearray(s.sent[q])
            raw[0] |= 0x04 << 3
            self.raw_send(s, bytes(raw))
            self.stats['resent'] += 1
        s.next_resend = time.monotonic() + RESEND_EVERY

    def established(self):
        return [s for s in self.sessions.values() if s.sid is not None]

    def broadcast(self, payload):
        for o in self.established():
            self.send_reliable(o, payload)

    def handle(self, data, addr):
        if len(data) < 12 or time.monotonic() < self.down_until:
            return
        hdr = struct.unpack('>HHHHHH', data[:12])
        flags = hdr[0] >> 11
        if flags & 0x02:
            if len(data) > 12 and data[12] == 0x01:
                old = self.sessions.pop(addr, None)
                if old is not None and old.sid is not None:
                    self.end_session(old, 'rehello')
                if len(self.established()) >= self.max_sessions:
                    self.stats['refused'] += 1
                    self.sock.sendto(struct.pack('>HHHHHH', (0x02 << 11) | 20, hdr[1], 0, 0, 0, 0)
                                     + bytes([0x03, 0, 0, 0, 0, 0, 0, 0]), addr)
                    return
                self.sessions[addr] = Session(addr)
                self.sock.sendto(struct.pack('>HHHHHH', (0x02 << 11) | 20, hdr[1], 0, 0, 0, 0)
                                 + bytes([0x02, 0, 0, 0, 0, 0, 0, 0]), addr)
            elif len(data) > 12 and data[12] == 0x04:
                s = self.sessions.get(addr)
                if s is not None:
                    self.stats['goodbyes'] += 1
                    self.end_session(s, 'goodbye')
            return
        s = self.sessions.get(addr)
        if s is None:
            return
        s.last_heard = time.monotonic()
        if s.sid is None:
            if flags & 0x10:
                s.sid = self.next_sid
                self.next_sid = 0x8001 + ((self.next_sid + 1 - 0x8001) & 0x7ffe)
                self.stats['sessions'] += 1
                self.dump(s)
            return
        if hdr[1] != s.sid:
            return
        if flags & 0x10:
            self.on_ack(s, hdr[2])
        if flags & 0x08 and not flags & 0x01:
            # The client asks for ours from hdr[3] on.
            pass
        if flags & 0x01:
            seq = hdr[5]
            if s.client_seq is None or seq == (s.client_seq + 1) & 0x7fff:
                s.client_seq = seq
                i = 12
                while i + 8 <= len(data):
                    ln = struct.unpack('>H', data[i:i + 2])[0]
                    if ln < 8:
                        break
                    self.command(s, data[i + 4:i + 8], data[i + 8:i + ln])
                    if s.addr not in self.sessions:
                        return
                    i += ln
            elif ((seq - s.client_seq) & 0x7fff) < 0x4000:
                self.stats['client_dropped_ooo'] += 1
                want = (s.client_seq + 1) & 0x7fff
                now = time.monotonic()
                if self.rtx_request and (s.rtx_asked != want or now - s.rtx_at > 0.08):
                    s.rtx_asked, s.rtx_at = want, now
                    self.stats['rtx_requests'] += 1
                    self.raw_send(s, struct.pack('>HHHHHH', (0x08 << 11) | 12, s.sid, 0, want, 0, 0))
            else:
                self.stats['client_dup'] += 1
            s.unacked_client += 1
            if s.unacked_client >= 16:
                self.send_ack(s)
            elif s.ack_timer is None:
                s.ack_timer = time.monotonic() + 0.01

    def dump(self, s):
        """The state dump, packed as the units pack it."""
        mode = 16 if self.width > 1920 else 10
        cmds = [cmd(b'_ver', struct.pack('>HH', 2, 30)),
                cmd(b'_pin', b'ATEM Fake'.ljust(44, b'\0')),
                cmd(b'VidM', bytes([mode, 0, 0, 0])),
                cmd(b'_mpl', bytes([20, 0, 100, 0]))]
        cmds += [inpr(i, *self.labels[i]) for i in sorted(self.labels)]
        cmds += [cmd(b'PrgI', struct.pack('>BxH', 0, self.program)),
                 cmd(b'PrvI', struct.pack('>BxH4x', 0, self.preview))]
        cmds += [cmd(b'MvIn', struct.pack('>BBH??2x', 0, w, src, True, True)) for w, src in sorted(self.mv.items())]
        for i in range(20):
            st = self.stills.get(i)
            cmds.append(mpfe(i, st is not None, st[0] if st else bytes(16)))
        for i in range(100):
            m = self.macros.get(i)
            cmds.append(mprp(i, m is not None, m[0] if m else b''))
        have = sum(len(c) for c in cmds)
        pad = cmd(b'_FkP', bytes(120))   # a command the client has no reader for
        while have < self.dump_kb * 1000:
            cmds.append(pad)
            have += len(pad)
        packet = b''
        for c in cmds:
            if len(packet) + len(c) > DGRAM_PAYLOAD:
                self.send_reliable(s, packet)
                packet = b''
            packet += c
        self.send_reliable(s, packet + cmd(b'InCm', b'\x01\x00\x00\x00'))

    def end_session(self, s, why):
        self.sessions.pop(s.addr, None)
        if why == 'reaped':
            self.stats['reaped'] += 1
        for w in self.waiters.values():
            if s in w:
                w.remove(s)
        if self.transfer is not None and self.transfer['s'] is s:
            self.transfer = None
        for store, h in list(self.holder.items()):
            if h is s:
                self.release(store)

    # --- locks ---
    def release(self, store):
        self.holder.pop(store, None)
        self.lkst_pending.append(struct.pack('>HBB', store, 0, 0xff))
        w = self.waiters.get(store) or []
        while w:
            nxt = w.pop(0)
            if nxt.addr in self.sessions:
                self.grant(store, nxt)
                break

    def grant(self, store, s):
        self.holder[store] = s
        self.send_reliable(s, cmd(b'LKOB', struct.pack('>H2x', store)))
        self.lkst_pending.append(struct.pack('>HBB', store, 1, 0xff))

    def ftde(self, s, tid, code):
        self.stats['ftde'][str(code)] = self.stats['ftde'].get(str(code), 0) + 1
        self.send_reliable(s, cmd(b'FTDE', struct.pack('>HBx', tid, code)))

    # --- commands ---
    def command(self, s, name, body):
        n = name.decode('latin-1')
        self.stats['commands'][n] = self.stats['commands'].get(n, 0) + 1
        if name in (b'PLCK', b'LOCK'):
            store = struct.unpack('>H', body[:2])[0]
            on = 1 if name == b'PLCK' else body[2]
            if on:
                h = self.holder.get(store)
                if h is None:
                    self.grant(store, s)
                elif h is not s and s not in self.waiters.setdefault(store, []):
                    self.waiters[store].append(s)
            elif self.holder.get(store) is s:
                if self.transfer is not None and self.transfer['s'] is s and self.transfer.get('store') == store:
                    self.transfer = None
                self.release(store)
        elif name == b'CPgI':
            self.program = struct.unpack('>H', body[2:4])[0]
            self.broadcast(cmd(b'PrgI', struct.pack('>BxH', 0, self.program)))
        elif name == b'CPvI':
            self.preview = struct.unpack('>H', body[2:4])[0]
            self.broadcast(cmd(b'PrvI', struct.pack('>BxH4x', 0, self.preview)))
        elif name == b'CInL':
            mask, src = body[0], struct.unpack('>H', body[2:4])[0]
            long_, short = self.labels.get(src, ('', ''))
            if mask & 1:
                long_ = body[4:24].split(b'\0')[0].decode('utf-8', 'replace')
            if mask & 2:
                short = body[24:28].split(b'\0')[0].decode('utf-8', 'replace')
            self.labels[src] = (long_, short)
            self.broadcast(inpr(src, long_, short))
        elif name == b'CMvI':
            mv, win, src = struct.unpack('>BBH', body[:4])
            self.mv[win] = src
            self.broadcast(cmd(b'MvIn', struct.pack('>BBH??2x', mv, win, src, True, True)))
        elif name == b'FTSD':
            tid, store = struct.unpack('>HH', body[:4])
            slot = struct.unpack('>H', body[6:8])[0]
            length = struct.unpack('>I', body[8:12])[0]
            if self.transfer is not None:
                return self.ftde(s, tid, 1)
            if store != 0xffff and self.holder.get(store) is not s:
                return self.ftde(s, tid, 5)
            self.transfer = {'s': s, 'tid': tid, 'up': True, 'store': store, 'slot': slot,
                             'len': length, 'got': 0, 'left': 0, 'ftfd': None, 'data': [],
                             't0': time.monotonic(), 'done': False}
            self.stats['uploads'].append({'store': store, 'slot': slot, 'len': length,
                                          'at': round(time.monotonic() - self.t0, 3)})
            self.later(0.005, self.ftcd)
            if store == 0xffff:
                self.macros[slot] = (b'', b'')
                self.broadcast(mprp(slot, True))
        elif name == b'FTDa':
            tid, size = struct.unpack('>HH', body[:4])
            t = self.transfer
            if t is None or not t.get('up') or t['tid'] != tid or t['s'] is not s:
                return
            t['left'] -= 1
            piece = bytes(body[4:4 + size])
            t['got'] += decoded_len(piece) if t['store'] == 0 else size
            t['data'].append(piece)
            if t['got'] >= t['len']:
                self.maybe_done()
            elif t['left'] == 0:
                self.later(0.005, self.ftcd)
        elif name == b'FTFD':
            tid = struct.unpack('>H', body[:2])[0]
            t = self.transfer
            if t is not None and t.get('up') and t['tid'] == tid:
                t['name'] = body[2:66].split(b'\0')[0]
                t['ftfd'] = body[194:210]
                self.maybe_done()
        elif name == b'FTSU':
            tid, store, slot = struct.unpack('>HHI', body[:8])
            if self.transfer is not None:
                return self.ftde(s, tid, 1)
            if store == 0xffff:
                m = self.macros.get(slot)
                data = m[1] if m else None
            else:
                if self.holder.get(store) is not s:
                    return self.ftde(s, tid, 5)
                st = self.stills.get(slot)
                data = st[1] if st else None
            if not data:
                return self.ftde(s, tid, 2)
            self.transfer = {'s': s, 'tid': tid, 'store': store, 'data': data, 'off': 0, 'sent': 0,
                             'ftua': 0, 'ready': time.monotonic() + self.rng.uniform(*self.prep)}
            self.stats['downloads'] += 1
            self.later(self.transfer['ready'] - time.monotonic(), self.pump)
        elif name == b'FTUA':
            tid = struct.unpack('>H', body[:2])[0]
            t = self.transfer
            if t is not None and not t.get('up') and t['s'] is s and t['tid'] == tid:
                t['ftua'] += 1
                self.pump()
        elif name == b'MAct':
            idx, action = struct.unpack('>HB', body[:3])
            if action == 5 and idx in self.macros:
                del self.macros[idx]
                self.broadcast(mprp(idx, False))
        elif name == b'CSTL':
            if self.holder.get(0) is s or True:
                slot = body[0]
                self.stills.pop(slot, None)
                self.broadcast(mpfe(slot, False))

    def later(self, dt, fn):
        self._tn += 1
        heapq.heappush(self.timers, (time.monotonic() + max(0.0, dt), self._tn, fn))

    def ftcd(self):
        t = self.transfer
        if t is None or not t.get('up') or t['done'] or t['got'] >= t['len']:
            return
        t['left'] = self.budget
        self.send_reliable(t['s'], cmd(b'FTCD', struct.pack('>H4xHH2x', t['tid'], CHUNK, self.budget)))

    def maybe_done(self):
        t = self.transfer
        if t is None or t['done'] or t['got'] < t['len'] or t['ftfd'] is None:
            return
        t['done'] = True
        raw = b''.join(t['data'])
        plain = rle_decode(raw) if t['store'] == 0 else raw
        ok = hashlib.md5(plain).digest() == t['ftfd'] if t['store'] == 0 else True
        log = self.stats['uploads'][-1]
        log.update(s=round(time.monotonic() - t['t0'], 3), md5_ok=ok, bytes=len(raw))
        self.transfer = None
        extra = b''
        if t['store'] == 0xffff:
            self.macros[t['slot']] = (t.get('name') or b'', raw)
            extra = mprp(t['slot'], True, t.get('name') or b'')
        elif t['store'] == 0:
            self.stills[t['slot']] = (t['ftfd'], raw)
            extra = mpfe(t['slot'], True, t['ftfd'], t.get('name') or b'')
        self.broadcast(cmd(b'FTDC', struct.pack('>HBB', t['tid'], 1, 2) + b'\0' * 4) + extra)

    def pump(self):
        t = self.transfer
        if t is None or t.get('up') or time.monotonic() < t['ready']:
            return
        s, data = t['s'], t['data']
        if s.addr not in self.sessions:
            self.transfer = None
            return
        while t['off'] < len(data) and t['sent'] - t['ftua'] < FTUA_WINDOW:
            piece = data[t['off']:t['off'] + CHUNK]
            t['off'] += len(piece)
            t['sent'] += 1
            self.send_reliable(s, cmd(b'FTDa', struct.pack('>HH', t['tid'], len(piece)) + piece))
        if t['off'] >= len(data) and t['ftua'] >= t['sent']:
            self.transfer = None
            self.broadcast(cmd(b'FTDC', struct.pack('>HBB', t['tid'], 1, 2) + b'\0' * 4))

    # --- loop ---
    def step(self, timeout):
        now = time.monotonic()
        due = [now + timeout, self.next_tick]
        for s in self.sessions.values():
            if s.next_resend:
                due.append(s.next_resend)
            if s.ack_timer:
                due.append(s.ack_timer)
        if self.timers:
            due.append(self.timers[0][0])
        try:
            r, _, _ = select.select([self.sock], [], [], max(0.0, min(due) - now))
        except (OSError, ValueError):
            return False
        with self.lock:
            if r:
                self.sock.setblocking(False)
                for _ in range(512):
                    try:
                        data, addr = self.sock.recvfrom(2048)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    self.handle(data, addr)
            now = time.monotonic()
            if now < self.down_until:
                return True
            while self.timers and self.timers[0][0] <= now:
                _, _, fn = heapq.heappop(self.timers)
                fn()
            for s in list(self.sessions.values()):
                if s.ack_timer and now >= s.ack_timer:
                    self.send_ack(s)
                if s.next_resend and now >= s.next_resend and s.sent:
                    self.resend(s)
                if s.sid is not None and now - s.last_heard > self.reap:
                    self.end_session(s, 'reaped')
            if now >= self.next_tick:
                self.next_tick = now + TICK
                lk = b''.join(cmd(b'LKST', x) for x in self.lkst_pending)
                self.lkst_pending = []
                for s in self.established():
                    self.send_reliable(s, cmd(b'Time', b'\0' * 8) + lk)
        return True


def main(argv=None):
    """The switcher side as a process of its own: the fake, and in front of
    it the relay (``netlink``) unless ``--no-relay``. Prints ``ready
    <host> <port>`` (where the client connects), then takes control lines
    on stdin: ``reboot S``, ``cut S`` (0 plugs back), ``stats``, ``quit``."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import netlink
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--no-relay', action='store_true')
    ap.add_argument('--profile', default='{}', help='JSON: up/down Path kwargs')
    ap.add_argument('--fake', default='{}', help='JSON: Fake kwargs')
    ap.add_argument('--seconds', type=float, default=3600)
    a = ap.parse_args(argv)
    prof = json.loads(a.profile)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
    sock.bind((a.host, 0 if not a.no_relay else a.port))
    netlink.quiet_on_windows(sock)
    fake = Fake(sock, **json.loads(a.fake))
    relay = None
    if a.no_relay:
        where = sock.getsockname()
    else:
        relay = netlink.Relay(sock.getsockname(), netlink.Path(**prof.get('up', {})),
                              netlink.Path(**{'seed': 2, **prof.get('down', {})}),
                              listen=(a.host, a.port)).start()
        where = relay.addr
    print('ready %s %d' % where, flush=True)

    def control():
        for line in sys.stdin:
            parts = line.split()
            if not parts:
                continue
            with fake.lock:
                if parts[0] == 'reboot':
                    fake.reboot(float(parts[1]))
                elif parts[0] == 'cut' and relay is not None:
                    secs = float(parts[1])
                    relay.cut(secs > 0)
                    if secs > 0:
                        threading.Timer(secs, relay.cut, (False,)).start()
                elif parts[0] == 'mtu' and relay is not None:
                    relay.up.max_dgram = int(parts[1])
                elif parts[0] == 'stats':
                    out = dict(fake.stats)
                    if relay is not None:
                        out['link'] = {'up': relay.up.stats, 'down': relay.down.stats}
                    out['macros'] = {k: v[0].decode('latin-1') for k, v in fake.macros.items()}
                    out['stills'] = sorted(fake.stills)
                    out['sessions_now'] = len(fake.established())
                    print('stats ' + json.dumps(out), flush=True)
                elif parts[0] == 'quit':
                    os._exit(0)
        os._exit(0)
    threading.Thread(target=control, daemon=True).start()
    end = time.monotonic() + a.seconds
    while time.monotonic() < end:
        fake.step(0.05)


if __name__ == '__main__':
    main()
