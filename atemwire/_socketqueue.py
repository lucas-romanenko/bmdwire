# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
import queue
import select
import socket
import os


class SocketQueue(queue.Queue):
    """
    This is a queue.Queue that's also a socket so it works with the select() call
    to await both a queue item and a network packet.
    """

    def __init__(self):
        super().__init__()

        if os.name == 'posix':
            self._putsocket, self._getsocket = socket.socketpair()
        else:
            # Compatibility on non-POSIX systems
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            self._putsocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._putsocket.connect(server.getsockname())
            self._getsocket, _ = server.accept()
            server.close()

    def fileno(self):
        return self._getsocket.fileno()

    def put(self, item, **kwargs):
        super().put(item, **kwargs)
        self._putsocket.send(b'x')

    def get(self, **kwargs):
        self._getsocket.recv(1)
        return super().get(**kwargs)

    def get_waiting(self):
        """Every item put so far, without blocking: one read of the wakeup
        bytes, then that many items. For a reader woken by select() that
        should empty the queue in one go instead of one item per wakeup.

        select() asks whether there is anything to read, since Windows has
        no socket.MSG_DONTWAIT: with the flag the UDP thread ended at its
        first send there and no switcher connected (1.11.0 to 1.14.2). Over
        the loopback TCP pair of a non-POSIX system the read can return
        fewer bytes than were put; the rest are readable at the next wakeup
        and no item is lost."""
        if not select.select([self._getsocket], [], [], 0)[0]:
            return []
        try:
            marks = self._getsocket.recv(4096)
        except (BlockingIOError, InterruptedError):
            return []
        return [super(SocketQueue, self).get() for _ in range(len(marks))]

    def close(self):
        """Free the socketpair FDs. Idempotent. Without this the pair was
        GC-only — every leaked protocol pinned 2 FDs (2026-07-06)."""
        for s in (self._putsocket, self._getsocket):
            try:
                s.close()
            except OSError:
                pass
