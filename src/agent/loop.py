"""The wake loop — the Linux kernel parks and unparks the agent.

One epoll over the agent's own fds; no daemon of ours routes events.
Two edges at v4.0 (the console socket joins with the console re-plumb):

  - inotify on the member's inbox spool — a delivered file is a wake
  - a timerfd for the next scheduled task — expiry is a wake

Between waits the process costs nothing: it is blocked in epoll_wait.
Events that arrive mid-turn queue in the fds and surface on the next
wait; only files delivered while the process was *down* need a boot
scan of the inbox (the caller's job).
"""

from __future__ import annotations

import ctypes
import errno
import os
import select
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

_libc = ctypes.CDLL(None, use_errno=True)

# <sys/inotify.h>; NONBLOCK/CLOEXEC match O_* on all Linux arches we run.
_IN_CLOEXEC = 0o2000000
_IN_NONBLOCK = 0o4000
_IN_CLOSE_WRITE = 0x008
_IN_MOVED_TO = 0x080  # atomic delivery: write elsewhere, rename in

_EVENT_HDR = struct.Struct("iIII")  # wd, mask, cookie, len

# <linux/timerfd.h>; not exposed by the os module (only TFD_TIMER_ABSTIME is).
_TFD_TIMER_CANCEL_ON_SET = 2


def _inotify_fd(watch: Path) -> int:
    fd = _libc.inotify_init1(_IN_CLOEXEC | _IN_NONBLOCK)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    wd = _libc.inotify_add_watch(
        fd, os.fsencode(watch), _IN_CLOSE_WRITE | _IN_MOVED_TO
    )
    if wd < 0:
        err = ctypes.get_errno()
        os.close(fd)
        raise OSError(err, f"inotify_add_watch failed for {watch}")
    return fd


def _drain_names(fd: int) -> list[str]:
    names: list[str] = []
    while True:
        try:
            buf = os.read(fd, 4096)
        except BlockingIOError:
            return names
        offset = 0
        while offset < len(buf):
            _, _, _, name_len = _EVENT_HDR.unpack_from(buf, offset)
            offset += _EVENT_HDR.size
            name = buf[offset : offset + name_len].split(b"\0", 1)[0]
            offset += name_len
            if name:
                names.append(os.fsdecode(name))


@dataclass
class Wake:
    inbox: list[Path] = field(default_factory=list)
    timer: bool = False


class WakeLoop:
    """Blocks the process until the OS has a reason to run it."""

    def __init__(self, inbox: Path):
        self._inbox_dir = inbox
        self._inotify = _inotify_fd(inbox)
        # REALTIME + CANCEL_ON_SET: schedules are wall-clock, and a host
        # clock jump surfaces as a wake so the caller re-arms.
        self._timer = os.timerfd_create(
            time.CLOCK_REALTIME, flags=os.TFD_NONBLOCK | os.TFD_CLOEXEC
        )
        self._epoll = select.epoll()
        self._epoll.register(self._inotify, select.EPOLLIN)
        self._epoll.register(self._timer, select.EPOLLIN)

    def arm(self, when: float) -> None:
        """Arm the scheduled-task edge at an absolute unix time."""
        os.timerfd_settime(
            self._timer,
            flags=os.TFD_TIMER_ABSTIME | _TFD_TIMER_CANCEL_ON_SET,
            initial=when,
        )

    def disarm(self) -> None:
        os.timerfd_settime(self._timer, initial=0.0)

    def wait(self, timeout: float = -1.0) -> Wake | None:
        """Sleep until an edge fires; None only on timeout (tests/shutdown)."""
        events = self._epoll.poll(timeout)
        if not events:
            return None
        wake = Wake()
        for fd, _ in events:
            if fd == self._inotify:
                wake.inbox = [self._inbox_dir / n for n in _drain_names(fd)]
            elif fd == self._timer:
                try:
                    os.read(fd, 8)
                except BlockingIOError:
                    pass
                except OSError as exc:  # clock jumped under CANCEL_ON_SET
                    if exc.errno != errno.ECANCELED:
                        raise
                wake.timer = True
        return wake

    def close(self) -> None:
        self._epoll.close()
        os.close(self._inotify)
        os.close(self._timer)
