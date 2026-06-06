"""System-wide hotkey via Carbon's RegisterEventHotKey.

Why Carbon and not an NSEvent global monitor: the Carbon hot-key API needs no
Accessibility permission and *consumes* the keystroke, where a global NSEvent
monitor needs Accessibility and only observes. We load Carbon through ctypes so
the app stays dependency-free (keeps the thin-launcher / git-pull model).

The handler is installed on the application event target, so it fires on the
main run loop — the same thread NSApp/rumps run on — which means the callback
can touch AppKit windows directly without dispatching.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_int32, c_uint32, c_void_p

# Carbon modifier masks (Events.h)
CMD_KEY = 0x0100
SHIFT_KEY = 0x0200
OPTION_KEY = 0x0800
CONTROL_KEY = 0x1000

# Virtual key codes (Events.h, kVK_ANSI_*)
KEY_D = 0x02

_kEventClassKeyboard = 0x6B657962  # 'keyb'
_kEventHotKeyPressed = 5

_carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))


class _EventTypeSpec(Structure):
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


class _EventHotKeyID(Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


# OSStatus (*)(EventHandlerCallRef, EventRef, void*)
_HandlerProc = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)

_carbon.GetApplicationEventTarget.restype = c_void_p
_carbon.InstallEventHandler.argtypes = [
    c_void_p, _HandlerProc, c_uint32, POINTER(_EventTypeSpec), c_void_p, c_void_p,
]
_carbon.InstallEventHandler.restype = c_int32
_carbon.RegisterEventHotKey.argtypes = [
    c_uint32, c_uint32, _EventHotKeyID, c_void_p, c_uint32, POINTER(c_void_p),
]
_carbon.RegisterEventHotKey.restype = c_int32

# ctypes objects passed to the C side must outlive the call — a GC of the
# handler proc or the registration would crash the run loop when the key fires.
_REFS: list = []


def register(callback, key_code: int = KEY_D, modifiers: int = CMD_KEY | OPTION_KEY) -> bool:
    """Register a global hotkey (default ⌥⌘D).

    `callback` is a no-arg callable invoked on the main thread when the combo is
    pressed. Returns True on success, False if the OS rejected the registration
    (e.g. the combo is already claimed system-wide).
    """
    def _proc(_call_ref, _event, _user_data):
        try:
            callback()
        except Exception:
            pass
        return 0

    proc = _HandlerProc(_proc)
    spec = _EventTypeSpec(_kEventClassKeyboard, _kEventHotKeyPressed)
    target = _carbon.GetApplicationEventTarget()

    if _carbon.InstallEventHandler(target, proc, 1, byref(spec), None, None) != 0:
        return False

    hk_id = _EventHotKeyID(0x44535054, 1)  # signature 'DSPT'
    ref = c_void_p()
    if _carbon.RegisterEventHotKey(key_code, modifiers, hk_id, target, 0, byref(ref)) != 0:
        return False

    _REFS.extend([proc, spec, hk_id, ref])
    return True
