# SPDX-License-Identifier: LGPL-3.0-only
"""atemwire test-suite conftest — fixtures that install/configure the
test doubles from ``atemwire.testing`` (protocol factory, pool reset,
fast timeouts). Scoped here because only atemwire's own tests use them;
applications that test against the library import ``atemwire.testing`` directly.
"""

import pytest

from atemwire.testing import FakeAtemProtocol  # noqa: F401  (fixture plumbing)


@pytest.fixture
def fake_protocol_factory(monkeypatch):
    """Patch ``atemwire.connection.AtemProtocol`` to a fake. Returns a factory
    function so tests can configure the fake per-test.

    Usage:
        def test_x(fake_protocol_factory):
            p = fake_protocol_factory(connect_delay=0.05)
            # ATEMConnection.connect() will instantiate this fake
    """
    instances = []

    class _Factory:
        def __init__(self):
            self.ctor_kwargs = {}

        def __call__(self, **kwargs):
            self.ctor_kwargs = kwargs
            return None

        def make_class(self):
            ctor_kwargs = self.ctor_kwargs

            class _Configured(FakeAtemProtocol):
                def __init__(self, ip=None, **runtime_kwargs):
                    super().__init__(ip=ip, **ctor_kwargs)
                    instances.append(self)
            return _Configured

    factory = _Factory()

    def configure_and_install(**kwargs):
        factory(**kwargs)
        cls = factory.make_class()
        monkeypatch.setattr('atemwire.connection.AtemProtocol', cls)
        return instances  # the list grows as tests instantiate

    return configure_and_install


@pytest.fixture
def reset_pool():
    """Yield, then clear ATEMInstanceManager._instances. Each test gets a
    clean pool so ordering doesn't matter."""
    from atemwire.pool import ATEMInstanceManager
    # Defensive — a prior test might have leaked entries.
    with ATEMInstanceManager._instance_lock:
        for ip in list(ATEMInstanceManager._instances.keys()):
            entry = ATEMInstanceManager._instances[ip]
            timer = entry.get('disconnect_timer')
            if timer is not None:
                timer.cancel()
            try:
                entry['connection'].disconnect()
            except Exception:
                pass
            del ATEMInstanceManager._instances[ip]
    yield
    with ATEMInstanceManager._instance_lock:
        for ip in list(ATEMInstanceManager._instances.keys()):
            entry = ATEMInstanceManager._instances[ip]
            timer = entry.get('disconnect_timer')
            if timer is not None:
                timer.cancel()
            try:
                entry['connection'].disconnect()
            except Exception:
                pass
            del ATEMInstanceManager._instances[ip]


@pytest.fixture
def short_grace(monkeypatch):
    """Override DISCONNECT_GRACE_PERIOD for fast tests."""
    monkeypatch.setattr('atemwire.pool.DISCONNECT_GRACE_PERIOD', 0.1)


@pytest.fixture
def short_connect_timeout(monkeypatch):
    """Override CONNECT_TIMEOUT and DISCONNECT_JOIN_TIMEOUT for fast tests."""
    monkeypatch.setattr('atemwire.connection.CONNECT_TIMEOUT', 1.0)
    monkeypatch.setattr('atemwire.connection.DISCONNECT_JOIN_TIMEOUT', 0.5)
