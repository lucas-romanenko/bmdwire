# SPDX-License-Identifier: LGPL-3.0-only
"""
atemwire.atem — primary user-facing interface to a Blackmagic ATEM switcher.

One class, ``ATEM``, wrapping a pooled connection. Construct ``ATEM(ip)``,
call any operation as a method, read properties for common state, call
``snapshot()`` for a full state dict, and close via the context manager
or ``close()``.

Operation forwarding rule: any attribute access that isn't a property,
lifecycle method, or state reader defined below falls through to
``__getattr__``, which looks the name up in the ``_OPERATIONS`` dict
built at module load by walking the per-feature ``atemwire.messages``
modules. The returned callable closes over ``self`` and passes the
current ``_conn`` to the operation each time it's invoked.

Typical usage::

    from atemwire.atem import ATEM

    with ATEM('192.0.2.10') as atem:
        atem.cut()                       # → switching.cut(conn, ...)
        atem.set_program(5)              # → switching.set_program(conn, ...)
        print(atem.program_source, atem.video_mode)

Add a new operation by adding a function in the right
``atemwire/messages/<feature>.py`` module — no edit to this file is needed
for it to be reachable as ``ATEM.your_op()`` (the module-load namespace
walk picks it up). IDE auto-complete won't list the forwarded names;
ship an ``atem.pyi`` stub if that becomes a friction point.
"""

from typing import Optional

from atemwire._state import build_full_state, display_fps
from atemwire.connection import ATEMConnection, ConnectionDeadError
from atemwire.helpers import format_rate
from atemwire.messages import (
    color_generator as _m_color_generator,
    downstream_keyer as _m_downstream_keyer,
    fade_to_black as _m_fade_to_black,
    fairlight as _m_fairlight,
    input_video as _m_input_video,
    macros as _m_macros,
    media as _m_media,
    switching as _m_switching,
    system_info as _m_system_info,
    transition as _m_transition,
    upstream_keyer as _m_upstream_keyer,
)
from atemwire.messages.fade_to_black import ftb_active
from atemwire.messages.switching import preview_source, program_source
from atemwire.messages.system_info import product_name, video_mode
from atemwire.messages.transition import (
    active_transition_rate, transition_in_transition, transition_style,
)
from atemwire.pool import acquire_connection



# Build the operation namespace once at module load. Each
# ``atemwire.messages.<feature>`` module exports its operation wrappers as
# top-level functions; we walk the 10 feature modules with ops and
# collect every public callable whose ``__module__`` matches the feature
# (filtering out re-exported helpers like ``safe_int`` that were
# imported into the module's namespace).
_OPERATION_MODULES = (
    _m_switching, _m_color_generator, _m_fade_to_black, _m_media,
    _m_input_video, _m_upstream_keyer, _m_macros, _m_downstream_keyer,
    _m_transition, _m_fairlight,
)

_OPERATIONS: dict = {}
for _mod in _OPERATION_MODULES:
    for _name in dir(_mod):
        if _name.startswith('_'):
            continue
        _obj = getattr(_mod, _name)
        if callable(_obj) and getattr(_obj, '__module__', '') == _mod.__name__:
            _OPERATIONS[_name] = _obj
del _mod, _name, _obj


class ATEM:
    """Pooled session to one ATEM switcher.

    Construction acquires a pooled ``ATEMConnection`` for the given IP. If
    no other caller holds a reference, a fresh UDP socket is opened and the
    initial state dump is awaited (up to the pool's connect timeout). If a
    session is already pooled for this IP, construction joins it
    immediately. Closing releases this instance's pool reference; the
    underlying socket is torn down after the pool's grace period only when
    the last reference goes away.

    Prefer the context manager form::

        with ATEM(ip) as atem:
            ...

    An explicit ``close()`` is supported. Don't rely on ``__del__`` to
    release — CPython's refcount GC is usually prompt, but there's no
    guarantee.

    Threading:
        Operation methods (``cut``, ``set_program``, ``toggle_usk``, ...)
        are thread-safe and non-blocking — each enqueues a command on the
        connection's worker thread and returns. State property/method
        reads are safe to call from any thread; they read the live
        mixerstate dict under the GIL, so a read may see a state that's
        a few ms stale.

    Raised exceptions:
        Constructor raises ``RuntimeError`` on connect failure (timeout,
        unreachable host). The pool ref is released before the exception
        propagates, so no leak.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, ip: str):
        self._ip = ip
        self._cm = acquire_connection(ip)
        # If connect fails the pool's own try/finally releases the ref
        # before re-raising.
        self._conn: Optional[ATEMConnection] = self._cm.__enter__()

    def close(self) -> None:
        """Release this instance's pool reference. Idempotent — a second
        call is a no-op. After ``close()``, operation calls will raise via
        the connection layer; the ``connected`` property returns False."""
        if self._conn is None:
            return
        self._conn = None
        self._cm.__exit__(None, None, None)

    def __enter__(self) -> "ATEM":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Operation forwarding
    # ------------------------------------------------------------------

    def __getattr__(self, name: str):
        """Forward operation calls to the per-feature messages modules.

        Only fires for names not defined on the instance or class — so
        properties, lifecycle methods, and explicit state readers below
        take precedence. Raises ``AttributeError`` if no matching
        callable exists in the unified ``_OPERATIONS`` namespace (same
        shape as a regular missing-attribute error).

        Conn is resolved at call-time inside the returned closure, so a
        ``close()`` between attribute lookup and invocation raises
        ``ConnectionDeadError`` (guarded below — ``close()`` nulls
        ``_conn``, which would otherwise surface as a confusing
        ``AttributeError`` on ``None``).
        """
        op = _OPERATIONS.get(name)
        if op is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )

        def _forward(*args, **kwargs):
            conn = self._conn
            if conn is None:
                raise ConnectionDeadError(
                    f"ATEM({self._ip}) is closed")
            return op(conn, *args, **kwargs)

        _forward.__name__ = name
        _forward.__qualname__ = f"{type(self).__name__}.{name}"
        _forward.__doc__ = getattr(op, '__doc__', None)
        return _forward

    # ------------------------------------------------------------------
    # Overrides of same-named messages ops (explicit members win over
    # __getattr__ forwarding)
    # ------------------------------------------------------------------

    def clear_still(self, slot: int, *, timeout: float = 5.0) -> None:
        """Clear a media-pool still slot — the LOCKED clear.

        Shadows the bare ``messages.media.clear_still`` op on purpose:
        some ATEM models (1 M/E Production Studio 4K, observed live
        2026-07-29) silently ignore a bare CSTL from a session that does
        not hold the still-store lock, so the facade routes every clear
        through ``ATEMConnection.clear_still`` (LOCK → LKOB → CSTL →
        per-frame release), which works on all models."""
        self._conn.clear_still(slot, timeout=timeout)

    # ------------------------------------------------------------------
    # Escape hatch
    # ------------------------------------------------------------------

    @property
    def raw(self) -> ATEMConnection:
        """The underlying ``ATEMConnection``. For advanced callers that
        need direct mixerstate reads, custom command sending, or
        transport-level introspection (e.g. ``raw.protocol.transport``
        for event registration by long-lived observers)."""
        return self._conn

    # ------------------------------------------------------------------
    # State — scalar properties (ME 0 / global)
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        conn = self._conn
        return bool(conn and conn.is_connected)

    @property
    def video_mode(self) -> str:
        """Video-mode label, e.g. ``'1080p50'``. Empty string before the
        first ``VidM`` packet arrives."""
        vm = video_mode(self._conn.mixerstate)
        return vm.get('format', '') if vm else ''

    @property
    def product_name(self) -> str:
        return product_name(self._conn.mixerstate, '')

    @property
    def program_source(self) -> int:
        return program_source(self._conn.mixerstate, 0)

    @property
    def preview_source(self) -> int:
        return preview_source(self._conn.mixerstate, 0)

    @property
    def in_transition(self) -> bool:
        return transition_in_transition(self._conn.mixerstate, 0)

    @property
    def transition_style(self) -> int:
        """Integer enum: 0=Mix, 1=Dip, 2=Wipe, 3=DVE, 4=Stinger."""
        return transition_style(self._conn.mixerstate, 0)

    @property
    def transition_rate(self) -> str:
        """``'seconds:frames'`` for the currently active transition style."""
        mx = self._conn.mixerstate
        return format_rate(
            active_transition_rate(mx, 0),
            display_fps(mx),
        )

    @property
    def ftb_active(self) -> bool:
        return ftb_active(self._conn.mixerstate, 0)

    # ------------------------------------------------------------------
    # State — methods (explicit ME index)
    # ------------------------------------------------------------------

    def get_program_source(self, me: int = 0) -> int:
        return program_source(self._conn.mixerstate, me)

    def get_preview_source(self, me: int = 0) -> int:
        return preview_source(self._conn.mixerstate, me)

    def get_in_transition(self, me: int = 0) -> bool:
        return transition_in_transition(self._conn.mixerstate, me)

    def get_transition_style(self, me: int = 0) -> int:
        return transition_style(self._conn.mixerstate, me)

    def get_transition_rate(self, me: int = 0) -> str:
        mx = self._conn.mixerstate
        return format_rate(
            active_transition_rate(mx, me),
            display_fps(mx),
        )

    def get_ftb_active(self, me: int = 0) -> bool:
        return ftb_active(self._conn.mixerstate, me)

    # ------------------------------------------------------------------
    # Full snapshot — equivalent to build_full_state(conn)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Assemble the full state dict an application pushes to its
        frontend. Same shape as ``atemwire._state.build_full_state``. Build
        time is a few ms for a typical ATEM; call on-demand rather than
        in a tight loop."""
        return build_full_state(self._conn)
