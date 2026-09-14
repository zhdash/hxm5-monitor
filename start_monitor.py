# -*- coding: utf-8 -*-
"""独立启动监控进程（脱离父进程，关掉终端也继续跑）"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYW = r"C:\Users\jones\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
SCRIPT = os.path.join(HERE, "hxm5_monitor.py")
PID_FILE = os.path.join(HERE, "monitor.pid")
CONSOLE_LOG = os.path.join(HERE, "console.log")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _say(msg):
    """pythonw 下 sys.stdout 为 None，安全输出"""
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except Exception:  # noqa
        pass


def _alive(pid):
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa
        return False


def already_running():
    if not os.path.exists(PID_FILE):
        return None
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except Exception:  # noqa
        return None
    return pid if _alive(pid) else None


def main():
    pid = already_running()
    if pid:
        _say("already running, PID %d" % pid)
        return 0
    if not os.path.exists(PYW):
        _say("pythonw not found: %s" % PYW)
        return 1

    logf = open(CONSOLE_LOG, "ab", buffering=0)
    args = dict(
        cwd=HERE,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=logf,
        close_fds=True,
    )
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    # 优先尝试跳出沙箱的 Job Object，失败则退回普通脱离方式
    for fl in (flags | CREATE_BREAKAWAY_FROM_JOB, flags):
        try:
            p = subprocess.Popen([PYW, SCRIPT], creationflags=fl, **args)
            _say("launched, pid %d (flags=0x%X)" % (p.pid, fl))
            return 0
        except OSError as e:
            _say("breakaway 失败(0x%X): %s" % (fl, e))
    return 1


if __name__ == "__main__":
    sys.exit(main())
