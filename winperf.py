"""Keep Windows from running the emulator (and the synth process) slowly in the background.

Windows 11 gives a program whose window is not in front "efficiency mode" treatment (EcoQoS): its threads are
moved to the slow efficiency cores of hybrid CPUs and run at a lower clock. The emulator is a console program
while the person looks at the browser, so it was always in the background. These calls opt the process and the
thread out of that, raise their priority, and ask for 1 ms timer resolution. On other systems they do nothing.
"""
import sys

IS_WIN = sys.platform == 'win32'
_done = {}


def _throttle_state():
    import ctypes
    class STATE(ctypes.Structure):
        _fields_ = [('Version', ctypes.c_ulong), ('ControlMask', ctypes.c_ulong), ('StateMask', ctypes.c_ulong)]
    # ControlMask EXECUTION_SPEED (1) | IGNORE_TIMER_RESOLUTION (4); StateMask 0 = never throttle
    return STATE(1, 1 | 4, 0)


def _k32():
    import ctypes
    from ctypes import wintypes as W
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    # handles are pointer-sized: declare the prototypes (a bare int would be passed as 32 bits)
    k.GetCurrentProcess.restype = W.HANDLE; k.GetCurrentThread.restype = W.HANDLE
    k.SetProcessInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]; k.SetProcessInformation.restype = W.BOOL
    k.SetThreadInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]; k.SetThreadInformation.restype = W.BOOL
    k.SetPriorityClass.argtypes = [W.HANDLE, W.DWORD]; k.SetPriorityClass.restype = W.BOOL
    k.SetThreadPriority.argtypes = [W.HANDLE, ctypes.c_int]; k.SetThreadPriority.restype = W.BOOL
    return k


def process(priority='above'):
    """Opt this process out of power throttling and raise its priority class. Returns a short description."""
    if not IS_WIN: return 'n/a'
    if 'process' in _done: return _done['process']
    import ctypes
    k = _k32(); out = []
    h = k.GetCurrentProcess()
    try:
        st = _throttle_state()
        ok = k.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))      # ProcessPowerThrottling
        out.append('no-eco' if ok else 'eco?')
    except Exception:
        out.append('eco?')
    try:
        cls = {'above': 0x8000, 'high': 0x80}.get(priority, 0x8000)
        out.append('prio-' + priority if k.SetPriorityClass(h, cls) else 'prio?')
    except Exception:
        out.append('prio?')
    try:
        ctypes.windll.winmm.timeBeginPeriod(1); out.append('timer1ms')
    except Exception:
        pass
    _done['process'] = ' '.join(out)
    return _done['process']


def thread(level='highest'):
    """For the calling thread: no power throttling, higher priority."""
    if not IS_WIN: return 'n/a'
    import ctypes
    k = _k32(); out = []
    h = k.GetCurrentThread()
    try:
        st = _throttle_state()
        ok = k.SetThreadInformation(h, 3, ctypes.byref(st), ctypes.sizeof(st))       # ThreadPowerThrottling
        out.append('no-eco' if ok else 'eco?')
    except Exception:
        out.append('eco?')
    try:
        lv = {'above': 1, 'highest': 2, 'critical': 15}.get(level, 2)
        out.append('prio-' + level if k.SetThreadPriority(h, lv) else 'prio?')
    except Exception:
        out.append('prio?')
    return ' '.join(out)
