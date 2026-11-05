"""Global hotkeys through Carbon's RegisterEventHotKey, called with ctypes.

Unlike an NSEvent global monitor, this needs no Accessibility permission, and the key press is
consumed rather than passed on to the front app. Callbacks run on the main thread.
"""

import ctypes
from ctypes import POINTER, byref, c_int32, c_uint32, c_void_p

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")

CMD, SHIFT, OPTION, CONTROL = 1 << 8, 1 << 9, 1 << 11, 1 << 12
# Virtual key codes (US layout positions)
KEYS = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
        "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
        "k": 40, "n": 45, "m": 46, "space": 49}
SYMBOLS = {CONTROL: "⌃", OPTION: "⌥", SHIFT: "⇧", CMD: "⌘"}


def _fourcc(s):
    return int.from_bytes(s.encode(), "big")


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


_Handler = ctypes.CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)

_carbon.GetApplicationEventTarget.restype = c_void_p
_carbon.InstallEventHandler.argtypes = [c_void_p, _Handler, c_uint32, POINTER(_EventTypeSpec), c_void_p,
                                        POINTER(c_void_p)]
_carbon.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, _EventHotKeyID, c_void_p, c_uint32, POINTER(c_void_p)]
_carbon.GetEventParameter.argtypes = [c_void_p, c_uint32, c_uint32, c_void_p, c_uint32, c_void_p, c_void_p]

_SIGNATURE = _fourcc("tdmb")
_callbacks = {}
_refs = []
_handler = None


@_Handler
def _on_event(_next, event, _data):
    hotkey = _EventHotKeyID()
    _carbon.GetEventParameter(event, _fourcc("----"), _fourcc("hkid"), None, ctypes.sizeof(hotkey), None,
                              byref(hotkey))
    callback = _callbacks.get(hotkey.id)
    if callback:
        try:
            callback()
        except Exception:
            import traceback
            traceback.print_exc()
    return 0


def label(key, modifiers):
    return "".join(s for m, s in SYMBOLS.items() if modifiers & m) + key.upper()


def register(key, modifiers, callback):
    """Register key (a name from KEYS) with modifier flags. Returns True, or False if it's taken."""
    global _handler
    if _handler is None:
        spec = _EventTypeSpec(_fourcc("keyb"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
        _handler = c_void_p()
        _carbon.InstallEventHandler(_carbon.GetApplicationEventTarget(), _on_event, 1, byref(spec), None,
                                    byref(_handler))
    hotkey_id = len(_callbacks) + 1
    ref = c_void_p()
    err = _carbon.RegisterEventHotKey(KEYS[key], modifiers, _EventHotKeyID(_SIGNATURE, hotkey_id),
                                      _carbon.GetApplicationEventTarget(), 0, byref(ref))
    if err:
        print(f"Hotkey {label(key, modifiers)} not registered (error {err}); another app may have it")
        return False
    _callbacks[hotkey_id] = callback
    _refs.append(ref)
    return True
