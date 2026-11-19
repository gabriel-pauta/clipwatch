"""Windows stand-in for the Unix ``fcntl`` module (native start.bat only).

openshorts is Linux-first and takes advisory file locks with ``flock``. Here
they map to ``msvcrt.locking`` on the file's first byte: exclusive, with
LOCK_SH treated as exclusive (the code only uses it for short reads).
shortcut: shared locks serialise readers; fine for one local user, use Docker for more.
"""
import msvcrt
import os
import time

LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
F_GETFD, F_SETFD, FD_CLOEXEC = 1, 2, 1


def _fd(f):
    return f if isinstance(f, int) else f.fileno()


def flock(f, op):
    fd = _fd(f)
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if op & LOCK_UN:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if op & LOCK_NB:
                    raise BlockingIOError(11, "Resource temporarily unavailable")
                time.sleep(0.05)
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def lockf(f, op, *args):
    return flock(f, op)


def fcntl(f, cmd, arg=0):
    return 0  # FD_CLOEXEC: Windows handles are not inherited by default


def ioctl(*args, **kwargs):
    raise OSError("ioctl is not available on Windows")
