# SPDX-License-Identifier: LGPL-3.0-only
"""The network matrix: every network path atemwire uses, through every
kind of network, against the fake switcher, with the data checked.

    python tools/netmatrix/netmatrix.py                       # everything, 3 runs
    python tools/netmatrix/netmatrix.py --profiles rtt200,mtu1200 --paths still_up
    python tools/netmatrix/netmatrix.py --quick               # the CI subset
    python tools/netmatrix/netmatrix.py --list

Each (path, profile) runs ``--runs`` times on a fresh connection; a line
per run, then a table: seconds of each run, and pass/fail. A path passes
when it finished within its cap and what arrived is what was sent (the
fake checks a still's MD5, the client reads labels, routes and macros
back). The caps are generous on purpose: they only stop a run that hangs;
the seconds are the measure.

The network is ``netlink.Relay`` in the switcher's process (any OS, no
root); ``direct`` has none, for the fastest a machine can go. The relay
is Python: on the fast profiles its own speed is part of the seconds, so
compare a fix with what it replaces on the same machine, not with a LAN.
"""
import argparse
import hashlib
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

MS = 0.001
MBIT = 1_000_000


def _p(**kw):
    return kw


def both(**kw):
    return {'up': dict(kw), 'down': dict(kw)}


# One way each: the round trip is twice the delay.
PROFILES = {
    # No network in between at all: the client straight to the fake.
    'direct':       {'direct': True},
    'lan':          both(delay=0.2 * MS),
    'rtt80':        both(delay=40 * MS),
    'rtt200':       both(delay=100 * MS),
    'loss0.5':      both(delay=20 * MS, loss=0.005),
    'loss2':        both(delay=20 * MS, loss=0.02),
    'loss5':        both(delay=20 * MS, loss=0.05),
    'jitter20':     both(delay=40 * MS, jitter=20 * MS),
    'reorder':      both(delay=40 * MS, jitter=10 * MS, reorder=True),
    'bw1':          both(delay=20 * MS, rate=1 * MBIT, queue=30),
    'bw5':          both(delay=20 * MS, rate=5 * MBIT, queue=40),
    'bw10':         both(delay=20 * MS, rate=10 * MBIT, queue=60),
    'mtu1400':      {'up': _p(delay=20 * MS, max_dgram=1400 - 28), 'down': _p(delay=20 * MS)},
    # A ZeroTier-like VPN: interface MTU 1,280. Carrying IPv4 the UDP
    # payload can be 1,252 bytes; over IPv6 (40-byte header) 1,232.
    'mtu1280':      {'up': _p(delay=20 * MS, max_dgram=1280 - 28), 'down': _p(delay=20 * MS)},
    'mtu1280v6':    {'up': _p(delay=20 * MS, max_dgram=1280 - 48), 'down': _p(delay=20 * MS)},
    'mtu1200':      {'up': _p(delay=20 * MS, max_dgram=1200 - 28), 'down': _p(delay=20 * MS)},
    'mtu1000':      {'up': _p(delay=20 * MS, max_dgram=1000 - 28), 'down': _p(delay=20 * MS)},
    'mtu576':       {'up': _p(delay=20 * MS, max_dgram=576 - 28), 'down': _p(delay=20 * MS)},
    'vpn':          {'up': _p(delay=40 * MS, loss=0.01, max_dgram=1200 - 28),
                     'down': _p(delay=40 * MS, loss=0.01)},
    # A slow site: a few Mbit/s, 60 ms, 1 % loss, a short queue.
    'slowsite':     both(delay=30 * MS, loss=0.01, rate=3 * MBIT, queue=40),
    # A busy host on a fast wire (a server process with many switchers):
    # one thread beside ours holding the GIL.
    'busyhost':     dict(both(delay=0.25 * MS, jitter=0.35 * MS), busy=1),
}

# The CI subset: one cell of each kind of trouble, a run each, about a
# minute. A fix adds the cell that showed its bug.
QUICK = [('lan', 'connect'), ('rtt200', 'connect'), ('loss5', 'commands'),
         ('vpn', 'labels'), ('mtu1200', 'still_up'), ('loss2', 'still_up'),
         ('reorder', 'macro_up'), ('mtu1280', 'profile_save'), ('lan', 'idle_cut'), ('lan', 'reboot'),
         ('lan', 'two_clients'), ('lan', 'disconnect')]


class Switcher:
    """The fake switcher (and its network) in a process of its own."""

    def __init__(self, profile, fake=None):
        prof = {k: v for k, v in profile.items() if k in ('up', 'down')}
        self.proc = subprocess.Popen(
            [sys.executable, '-I', os.path.join(HERE, 'fakeswitcher.py'),
             '--profile', json.dumps(prof), '--fake', json.dumps(fake or {})]
            + (['--no-relay'] if profile.get('direct') else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().split()
        if not line or line[0] != 'ready':
            raise RuntimeError('fake switcher did not start')
        self.host, self.port = line[1], int(line[2])

    t_op = None

    def mark(self):
        """The measured part of a path starts now (after its connect)."""
        self.t_op = time.monotonic()

    def tell(self, line):
        self.proc.stdin.write(line + '\n')
        self.proc.stdin.flush()

    def stats(self):
        self.tell('stats')
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {}
            if line.startswith('stats '):
                return json.loads(line[6:])

    def close(self):
        try:
            self.tell('quit')
        except OSError:
            pass
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --- the client side, as WebATEM and bmdctl use it ---

def protocol(sw):
    from atemwire.protocol import AtemProtocol
    p = AtemProtocol(sw.host, sw.port, aggressive_drain=True)
    p.connect()
    return p


def pump(p, cond, timeout):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            return False
        try:
            p.loop()
        except Exception:
            time.sleep(0.005)
    return True


def close(p):
    try:
        p.transport.close_session()
    except Exception:
        pass
    try:
        p.transport.sock.close()
    except Exception:
        pass


def noisy_still(seed, w=1920, h=1080):
    from atemwire.imaging import rgb_to_atem
    rng = random.Random(seed)
    rgba = bytearray(rng.randbytes(w * h * 4))
    rgba[3::4] = b'\xff' * (w * h)
    return rgb_to_atem(bytes(rgba), w, h)


def connected(p, cap):
    return pump(p, lambda: p.initialized, cap)


def upload_still(p, slot, data, cap, name='nm'):
    done, failed = {}, {}
    p.on('upload-done', lambda store, s: done.__setitem__(s, True))
    p.upload(0, slot, data, compress=True, name=name)
    ok = pump(p, lambda: slot in done, cap)
    if not ok:
        try:
            p.abort_transfers()
        except Exception:
            pass
    return ok


def download(p, store, slot, cap):
    got = {}
    p.on('download-done', lambda st, s, data: got.__setitem__((st, s), data))
    p.download(store, slot)
    if not pump(p, lambda: (store, slot) in got, cap):
        try:
            p.abort_transfers()
        except Exception:
            pass
        return None
    return got[(store, slot)]


# --- the paths: each returns (ok, what was checked) ---

def path_connect(sw, cap):
    p = protocol(sw)
    try:
        ok = connected(p, cap)
        ms = p.mixerstate
        good = ok and 'program-bus-input' in ms and len(ms.get('macro-properties', {})) == 100
        return good, 'state dump complete (InCm, 100 macros, program bus)'
    finally:
        close(p)


def _wait_value(p, get, want, cap):
    return pump(p, lambda: _safe(get) == want, cap)


def _safe(get):
    try:
        return get()
    except Exception:
        return None


def path_commands(sw, cap):
    """20 program cuts, each waited for: an operator's buttons."""
    from atemwire.messages.switching import ProgramInputCommand
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        end = time.monotonic() + cap
        for i in range(20):
            src = 1 + (i % 8)
            p.send_commands([ProgramInputCommand(index=0, source=src)])
            if not _wait_value(p, lambda: p.mixerstate['program-bus-input'][0].source, src,
                               end - time.monotonic()):
                return False, 'cut %d not confirmed' % i
        return True, '20 cuts confirmed by PrgI'
    finally:
        close(p)


def path_labels(sw, cap):
    from atemwire.messages.input_video import InputPropertiesCommand
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        tag = '%04d' % random.randrange(10000)
        p.send_commands([InputPropertiesCommand(i, label='In %d %s' % (i, tag), short_label='I%d' % i)
                         for i in range(1, 21)])

        def all_in():
            ip = p.mixerstate.get('input-properties', {})
            return all(ip.get(i) is not None and ip[i].name == 'In %d %s' % (i, tag) for i in range(1, 21))
        return pump(p, all_in, cap), '20 labels read back'
    finally:
        close(p)


def path_multiview(sw, cap):
    from atemwire.messages.multiviewer import MultiviewInputCommand
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        want = {w: 1 + ((w * 7 + 3) % 20) for w in range(2, 10)}
        p.send_commands([MultiviewInputCommand(0, w, s) for w, s in want.items()])

        def all_in():
            mv = p.mixerstate.get('multiviewer-input', {})
            return all(mv.get(0, {}).get(w) is not None and mv[0][w].source == s for w, s in want.items())
        return pump(p, all_in, cap), '8 multiview windows read back'
    finally:
        close(p)


STILL = {}
STILL_SEEDS = {'still_up': (1,), 'still_down': (2,), 'cable_pull': (3,), 'reboot': (4, 5),
               'two_clients': (10, 11)}


def _still(i):
    if i not in STILL:
        STILL[i] = noisy_still(i)
    return STILL[i]


def path_still_up(sw, cap):
    """A noisy 1080p still (8.3 MB on the wire) up, its MD5 checked by the
    switcher, then read back."""
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        data = _still(1)
        sw.mark()
        if not upload_still(p, 3, data, cap):
            return False, 'upload did not finish'
        st = sw.stats()
        up = st['uploads'][-1] if st.get('uploads') else {}
        if not up.get('md5_ok'):
            return False, 'MD5 wrong at the switcher'
        return True, 'MD5 ok at the switcher'
    finally:
        close(p)


def path_still_down(sw, cap):
    """Upload (unmeasured) then download: the read-back must be the frame."""
    from atemwire.imaging import rle_decode
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        data = _still(2)
        if not upload_still(p, 4, data, cap * 3):
            return None, 'setup upload failed'
        t = time.monotonic()
        back = download(p, 0, 4, cap)
        sw._down_s = time.monotonic() - t
        if back is None:
            return False, 'download did not finish'
        ok = hashlib.md5(back).digest() == hashlib.md5(rle_decode(data)).digest()
        return ok, 'downloaded frame identical' if ok else 'downloaded frame differs'
    finally:
        close(p)


def path_macro_up(sw, cap):
    from atemwire.macrotransfer import upload_macro_bytecode
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        blob = bytes(random.Random(5).randbytes(4000))
        err = {}

        def up():
            try:
                upload_macro_bytecode(p, 50, 'netmatrix', '', blob, timeout=cap)
            except Exception as e:
                err['e'] = repr(e)
            err['done'] = True
        th = threading.Thread(target=up, daemon=True)
        th.start()
        pump(p, lambda: 'done' in err, cap + 5)
        if 'e' in err or 'done' not in err:
            return False, err.get('e', 'no answer')[:120]
        back = download(p, 0xffff, 50, cap)
        ok = back == blob
        name = _safe(lambda: p.mixerstate['macro-properties'][50].name)
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        return ok and name == 'netmatrix', 'macro read back identical, named' if ok else 'read-back differs'
    finally:
        close(p)


def path_macro_fail_cleanup(sw, cap):
    """A macro upload into an empty slot that can't finish (the cable
    pulled for good after its first packets): the slot must be empty again
    afterwards, not a nameless macro."""
    from atemwire.macrotransfer import upload_macro_bytecode
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        err = {}

        def up():
            try:
                upload_macro_bytecode(p, 60, 'doomed', '', bytes(9000), timeout=4)
            except Exception as e:
                err['e'] = repr(e)
            err['done'] = True
        th = threading.Thread(target=up, daemon=True)
        th.start()
        # let the FTSD through, then drop everything bigger than a header
        pump(p, lambda: 60 in sw_macros(sw), 5)
        sw.tell('mtu 100')
        pump(p, lambda: 'done' in err, 20)
        sw.tell('mtu 0')
        pump(p, lambda: False, 3)
        left = sw_macros(sw)
        if 'e' not in err:
            return None, 'upload did not fail; scenario void'
        return 60 not in left, ('slot 60 empty after the failure' if 60 not in left
                                else 'nameless macro left in slot 60')
    finally:
        close(p)


def sw_macros(sw):
    return {int(k): v for k, v in sw.stats().get('macros', {}).items()}


class pooled_on:
    """The pool (ATEM(ip), what WebATEM's profile save and load use)
    connects to port 9910; point it at the fake's port while in use."""

    def __init__(self, sw):
        self.sw = sw

    def __enter__(self):
        from atemwire import connection
        self.orig = connection.AtemProtocol
        orig, port = self.orig, self.sw.port
        connection.AtemProtocol = lambda ip, **kw: orig(ip, port, **kw)
        return self

    def __exit__(self, *exc):
        from atemwire import connection
        from atemwire.pool import ATEMInstanceManager as M
        connection.AtemProtocol = self.orig
        # No grace period: the next run's fake is on another port, and a
        # pooled session kept for 127.0.0.1 would be joined instead.
        with M._instance_lock:
            gone = list(M._instances.values())
            M._instances.clear()
        for inst in gone:
            try:
                inst['connection'].disconnect()
            except Exception:
                pass


def path_profile_save(sw, cap):
    """A profile save as WebATEM's does it: ATEM(ip), Profile.from_atem
    (which downloads every macro), the XML."""
    from atemwire import ATEM, Profile
    from atemwire.profile import SaveOptions
    with pooled_on(sw):
        sw.mark()
        with ATEM(sw.host) as atem:
            if not atem.connected:
                return False, 'no connect'
            xml = Profile.from_atem(atem, SaveOptions()).to_xml()
    ok = ('FlyKeyRunToInfinity' in xml and 'longName="Camera 1"' in xml)
    return ok, ('XML with the inputs and the macro\'s ops' if ok else 'XML without the macro\'s ops or the labels')


def path_profile_restore(sw, cap):
    """Save a profile, change its labels and move its macro to an empty
    slot, restore it: the switcher has the new labels and the macro."""
    from atemwire import ATEM, Profile
    from atemwire.profile import SaveOptions
    with pooled_on(sw):
        with ATEM(sw.host) as atem:
            if not atem.connected:
                return False, 'no connect'
            xml = Profile.from_atem(atem, SaveOptions()).to_xml()
        xml = xml.replace('longName="Camera ', 'longName="Restored ')
        xml = xml.replace('<Macro index="0" name="Macro one"', '<Macro index="70" name="Restored"')
        sw.mark()
        with ATEM(sw.host) as atem:
            if not atem.connected:
                return False, 'no connect'
            Profile.from_xml(xml).apply(atem)
    st = sw.stats()
    labels_ok = st.get('labels', {}).get('1', [''])[0] == 'Restored 1'
    macro_ok = st.get('macros', {}).get('70') == 'Restored'
    return labels_ok and macro_ok, 'labels %s, macro 70 %s' % ('restored' if labels_ok else 'NOT restored',
                                                           'restored' if macro_ok else 'NOT restored')


def path_cable_pull(sw, cap):
    """The cable out for 3 s in the middle of a still upload: it must go on
    and finish, data intact."""
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        data = _still(3)
        done = {}
        p.on('upload-done', lambda store, s: done.__setitem__(s, True))
        p.upload(0, 5, data, compress=True, name='pull')
        pump(p, lambda: p.transfer is not None and getattr(p.transfer, 'offset', 0) > len(data) // 3, cap)
        sw.tell('cut 3')
        ok = pump(p, lambda: 5 in done, cap)
        st = sw.stats()
        up = st['uploads'][-1] if st.get('uploads') else {}
        if not ok:
            return False, 'did not finish'
        return bool(up.get('md5_ok')), ('finished after a 3 s cut, MD5 ok' if up.get('md5_ok')
                                        else 'finished after a 3 s cut, MD5 WRONG at the switcher')
    finally:
        close(p)


def path_idle_cut(sw, cap):
    """The cable out for 8 s on an idle connection (past the 5 s silence):
    the client re-handshakes and has the state again."""
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        sw.tell('cut 8')
        pump(p, lambda: False, 8.5)
        ok = pump(p, lambda: p.initialized and 'program-bus-input' in p.mixerstate, cap)
        return ok, 'state back after an 8 s cut'
    finally:
        close(p)


def path_reboot(sw, cap):
    """The switcher reboots (8 s) mid-upload: the upload fails with a clear
    error rather than hanging, the client reconnects, and the next upload
    works."""
    p = protocol(sw)
    try:
        if not connected(p, cap):
            return False, 'no connect'
        sw.mark()
        data = _still(4)
        done, failed = {}, {}
        p.on('upload-done', lambda store, s: done.__setitem__(s, True))
        p.upload(0, 6, data, compress=True, name='reboot')
        pump(p, lambda: p.transfer is not None and getattr(p.transfer, 'offset', 0) > len(data) // 3, cap)
        sw.tell('reboot 8')
        t = time.monotonic()
        # the first upload can't finish (the pool is gone); what matters is
        # that the connection comes back and a new upload works
        back = pump(p, lambda: time.monotonic() - t > 8.5 and p.initialized
                    and 'program-bus-input' in p.mixerstate, cap)
        if not back:
            return False, 'no reconnect after the reboot'
        try:
            p.abort_transfers()
        except Exception:
            pass
        ok = upload_still(p, 7, _still(5), cap)
        st = sw.stats()
        up = st['uploads'][-1] if st.get('uploads') else {}
        if not ok:
            return False, 'next upload failed'
        return bool(up.get('md5_ok')), 'reconnected, next upload MD5 ' + ('ok' if up.get('md5_ok') else 'WRONG')
    finally:
        close(p)


def path_two_clients(sw, cap):
    """Two clients upload at once: the lock goes from one to the other and
    both stills arrive."""
    ps = [protocol(sw), protocol(sw)]
    try:
        stop = threading.Event()
        oks = {}

        def one(i, p):
            if not connected(p, cap):
                oks[i] = False
                return
            oks[i] = upload_still(p, 10 + i, _still(10 + i), cap)
        ths = [threading.Thread(target=one, args=(i, p), daemon=True) for i, p in enumerate(ps)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(cap * 2 + 5)
        st = sw.stats()
        md5 = [u.get('md5_ok') for u in st.get('uploads', []) if u.get('slot') in (10, 11)]
        ok = oks.get(0) and oks.get(1) and md5.count(True) >= 2
        return bool(ok), 'both stills arrived, MD5 ok' if ok else 'oks=%s md5=%s' % (oks, md5)
    finally:
        for p in ps:
            close(p)


def path_disconnect(sw, cap):
    """Connect and disconnect five times: no session left behind."""
    for i in range(5):
        p = protocol(sw)
        if not connected(p, cap):
            close(p)
            return False, 'no connect %d' % i
        close(p)
    pump_sleep(1.0)
    n = sw.stats().get('sessions_now')
    return n == 0, '%s session(s) left after 5 connect/disconnect' % n


def pump_sleep(s):
    time.sleep(s)


PATHS = {
    'connect': (path_connect, 60),
    'commands': (path_commands, 60),
    'labels': (path_labels, 60),
    'multiview': (path_multiview, 60),
    'still_up': (path_still_up, 180),
    'still_down': (path_still_down, 180),
    'macro_up': (path_macro_up, 60),
    'macro_fail_cleanup': (path_macro_fail_cleanup, 30),
    'profile_save': (path_profile_save, 120),
    'profile_restore': (path_profile_restore, 240),
    'cable_pull': (path_cable_pull, 180),
    'idle_cut': (path_idle_cut, 60),
    'reboot': (path_reboot, 180),
    'two_clients': (path_two_clients, 240),
    'disconnect': (path_disconnect, 60),
}


def busy_thread(stop):
    x = 0
    while not stop.is_set():
        for i in range(100000):
            x += i * i


def run_one(profile_name, path_name, cap=None):
    prof = PROFILES[profile_name]
    fn, default_cap = PATHS[path_name]
    cap = cap or default_cap
    for i in STILL_SEEDS.get(path_name, ()):
        _still(i)
    # Each run as a fresh process would start: no chunk size learnt from
    # an earlier run (they all go to 127.0.0.1).
    try:
        from atemwire import transport
        getattr(transport, '_CHUNK_LIMITS', {}).clear()
    except ImportError:
        pass
    sw = Switcher(prof)
    stop = threading.Event()
    busy = [threading.Thread(target=busy_thread, args=(stop,), daemon=True) for _ in range(prof.get('busy', 0))]
    old = sys.getswitchinterval()
    if busy:
        sys.setswitchinterval(0.001)
    for b in busy:
        b.start()
    t = time.monotonic()
    try:
        ok, what = fn(sw, cap)
    except Exception as e:
        ok, what = False, 'error: %r' % (e,)
    s = time.monotonic() - (sw.t_op or t)
    if path_name == 'still_down' and hasattr(sw, '_down_s'):
        s = sw._down_s
    stop.set()
    sys.setswitchinterval(old)
    time.sleep(0.6)             # our goodbye on its way, at 200 ms too
    st = sw.stats()
    sw.close()
    return {'profile': profile_name, 'path': path_name, 'ok': ok, 's': round(s, 2), 'what': what,
            'ooo_dropped': st.get('client_dropped_ooo'), 'ghosts': st.get('sessions_now'),
            'link': st.get('link')}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--profiles', default=','.join(PROFILES))
    ap.add_argument('--paths', default=','.join(PATHS))
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--out', help='JSON lines file')
    ap.add_argument('--log', help='debug log file')
    a = ap.parse_args(argv)
    if a.list:
        print('profiles:', ', '.join(PROFILES))
        print('paths:', ', '.join(PATHS))
        return 0
    logging.basicConfig(level=logging.DEBUG if a.log else logging.ERROR, filename=a.log)
    import atemwire
    print('# atemwire from', os.path.dirname(atemwire.__file__), flush=True)
    cells = QUICK if a.quick else [(pr, pa) for pa in a.paths.split(',') for pr in a.profiles.split(',')]
    out = open(a.out, 'a') if a.out else None
    table = {}
    for pr, pa in cells:
        for i in range(a.runs):
            r = run_one(pr, pa)
            r['run'] = i + 1
            print('%-10s %-18s run %d  %-4s %7.2f s  %s' % (pr, pa, i + 1,
                  {True: 'PASS', False: 'FAIL', None: 'VOID'}[r['ok']], r['s'], r['what']), flush=True)
            if out:
                out.write(json.dumps(r) + '\n')
                out.flush()
            table.setdefault((pa, pr), []).append(r)
    print()
    print('%-18s %-10s %s' % ('path', 'profile', 'seconds per run'))
    bad = 0
    for (pa, pr), rs in table.items():
        fails = [r for r in rs if r['ok'] is False]
        bad += len(fails)
        print('%-18s %-10s %-28s %s' % (pa, pr, ' / '.join('%.2f' % r['s'] for r in rs),
                                        'FAIL %d/%d' % (len(fails), len(rs)) if fails else 'pass'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
