"""Windows stand-in for the Unix ``resource`` module (main.py's exit-time CPU report).
shortcut: reports zero usage on Windows; the report is informational only."""
from types import SimpleNamespace

RUSAGE_SELF, RUSAGE_CHILDREN = 0, -1


def getrusage(who):
    return SimpleNamespace(ru_utime=0.0, ru_stime=0.0, ru_maxrss=0)
