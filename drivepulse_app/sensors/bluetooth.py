"""Bluetooth RFCOMM-to-PTY bridge for DrivePulse."""
from __future__ import annotations

import os
import pty
import socket
import threading

from drivepulse_app.diagnostics import get_logger

log = get_logger(__name__)


class BluetoothPtyBridge:
    """Bridges a Bluetooth RFCOMM socket to a PTY so pyserial/python-obd can use it."""

    _CONNECT_TIMEOUT = 10.0
    # Bounded recv so the relay loop re-checks _stop instead of blocking forever.
    # socket.close() does NOT interrupt a thread parked in recv() — without this
    # (plus the shutdown() in close()) that thread leaks and wedges reconnects.
    _RECV_TIMEOUT = 1.0

    def __init__(self, addr: str, channel: int = 1) -> None:
        self.addr = addr
        self.channel = channel
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._master_fd = -1
        self._slave_fd = -1
        self.pty_path = ""
        self._open()

    def _open(self) -> None:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.settimeout(self._CONNECT_TIMEOUT)
            sock.connect((self.addr, self.channel))
            sock.settimeout(self._RECV_TIMEOUT)
        except Exception:
            sock.close()
            raise
        self._sock = sock
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._slave_fd = slave_fd
        self.pty_path = os.ttyname(slave_fd)
        threading.Thread(target=self._relay_fd_to_sock, args=(master_fd, sock), daemon=True).start()
        threading.Thread(target=self._relay_sock_to_fd, args=(sock, master_fd), daemon=True).start()

    def _relay_fd_to_sock(self, fd: int, sock: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    sock.sendall(data)
                except OSError as exc:
                    log.info("Bluetooth PTY->socket relay stopped: %s", exc)
                    break
        finally:
            self._stop.set()

    def _relay_sock_to_fd(self, sock: socket.socket, fd: int) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                    os.write(fd, data)
                except TimeoutError:
                    continue  # idle window elapsed → re-check _stop, keep relaying
                except OSError as exc:
                    log.info("Bluetooth socket->PTY relay stopped: %s", exc)
                    break
        finally:
            self._stop.set()

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                # close() alone never unblocks a concurrent recv() — shutdown does.
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # peer already gone / socket not connected
            try:
                self._sock.close()
            except OSError as exc:
                log.info("Could not close Bluetooth socket: %s", exc)
            self._sock = None
        for fd in (self._master_fd, self._slave_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as exc:
                    log.info("Could not close Bluetooth PTY fd %s: %s", fd, exc)
        self._master_fd = -1
        self._slave_fd = -1

    @property
    def is_alive(self) -> bool:
        return not self._stop.is_set()
