"""Read-only taskbar UI Automation queries and a background reading cache."""

from contextlib import ExitStack, contextmanager
import ctypes
import ctypes.wintypes as wintypes
import math
import threading
import time
import uuid


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text):
    value = uuid.UUID(text)
    data4 = (ctypes.c_ubyte * 8).from_buffer_copy(value.bytes[8:])
    return GUID(value.time_low, value.time_mid, value.time_hi_version, data4)


CLSID_CUIAutomation = _guid("FF48DBA4-60EF-4201-AA87-54103EEF594E")
IID_IUIAutomation = _guid("30CBE57D-D9D0-452A-AB13-7AC5AC4825EE")
COINIT_MULTITHREADED = 0
TREE_SCOPE_CHILDREN = 2
TREE_SCOPE_DESCENDANTS = 4


def rightmost(rects):
    """Return the greatest positive right edge of finite, nonempty rectangles."""
    right_edge = None
    if rects is None:
        return None
    for rect in rects:
        try:
            left, top, right, bottom = rect
            if not all(math.isfinite(value) for value in (left, top, right, bottom)):
                continue
            if right <= 0 or right <= left or bottom <= top:
                continue
            candidate = int(right)
        except (TypeError, ValueError, OverflowError):
            continue
        if candidate > 0 and (right_edge is None or candidate > right_edge):
            right_edge = candidate
    return right_edge


def _libraries():
    """Load and declare COM functions only when a native query is requested."""
    if not hasattr(ctypes, "WinDLL"):
        raise OSError("Windows UI Automation is unavailable")
    ole32 = ctypes.WinDLL("ole32")
    oleaut32 = ctypes.WinDLL("oleaut32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut32.SysFreeString.restype = None
    return ole32, oleaut32


def _check(hr, operation):
    if hr < 0:
        raise OSError("%s 0x%08X" % (operation, hr & 0xFFFFFFFF))


def _call(ptr, index, restype, *argtypes):
    if not ptr:
        raise OSError("UI Automation returned a null interface")
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(ptr):
    if ptr:
        _call(ptr, 2, ctypes.c_ulong)(ptr)


@contextmanager
def _interface():
    ptr = ctypes.c_void_p()
    try:
        yield ptr
    finally:
        _release(ptr)


def _owned(stack):
    return stack.enter_context(_interface())


def _automation_id(element, oleaut32):
    value = ctypes.c_void_p()
    try:
        hr = _call(element, 29, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            element, ctypes.byref(value))
        _check(hr, "get_CurrentAutomationId")
        return ctypes.wstring_at(value.value) if value.value else ""
    finally:
        if value.value:
            oleaut32.SysFreeString(value)


def _find_all(element, scope, condition, result):
    hr = _call(
        element, 6, ctypes.c_long, ctypes.c_int, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )(element, scope, condition, ctypes.byref(result))
    _check(hr, "FindAll")


def _length(array):
    count = ctypes.c_int()
    hr = _call(array, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
        array, ctypes.byref(count))
    _check(hr, "get_Length")
    if count.value < 0:
        raise OSError("UI Automation returned an invalid array length")
    return count.value


def _get_element(array, index, result):
    hr = _call(
        array, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
    )(array, index, ctypes.byref(result))
    _check(hr, "GetElement")


def query_group_right(hwnd):
    """Read TaskbarFrame's immediate children, balancing COM on this thread.

    The widget calls this from TaskbarProbe's worker. Direct diagnostic callers
    also get a COM apartment with no module-import or retained-interface state.
    """
    if not hwnd:
        return None
    ole32, oleaut32 = _libraries()
    _check(ole32.CoInitializeEx(None, COINIT_MULTITHREADED), "CoInitializeEx")
    try:
        with ExitStack() as stack:
            uia = _owned(stack)
            hr = ole32.CoCreateInstance(
                ctypes.byref(CLSID_CUIAutomation), None, 1,
                ctypes.byref(IID_IUIAutomation), ctypes.byref(uia))
            _check(hr, "CoCreateInstance")
            root = _owned(stack)
            hr = _call(uia, 6, ctypes.c_long, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p))(uia, hwnd, ctypes.byref(root))
            _check(hr, "ElementFromHandle")
            condition = _owned(stack)
            hr = _call(uia, 21, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                uia, ctypes.byref(condition))
            _check(hr, "CreateTrueCondition")
            descendants = _owned(stack)
            _find_all(root, TREE_SCOPE_DESCENDANTS, condition, descendants)
            frame = _owned(stack)
            for index in range(_length(descendants)):
                with _interface() as element:
                    _get_element(descendants, index, element)
                    if element and _automation_id(element, oleaut32) == "TaskbarFrame":
                        frame.value = element.value
                        element.value = None
                        break
            if not frame:
                return None
            children = _owned(stack)
            _find_all(frame, TREE_SCOPE_CHILDREN, condition, children)
            rects = []
            for index in range(_length(children)):
                with _interface() as element:
                    _get_element(children, index, element)
                    if not element:
                        continue
                    rect = wintypes.RECT()
                    hr = _call(element, 43, ctypes.c_long, ctypes.POINTER(wintypes.RECT))(
                        element, ctypes.byref(rect))
                    _check(hr, "get_CurrentBoundingRectangle")
                    rects.append((rect.left, rect.top, rect.right, rect.bottom))
            return rightmost(rects)
    finally:
        ole32.CoUninitialize()


class TaskbarProbe:
    """Poll UI Automation off-thread; readers never wait for a native query."""

    def __init__(self, get_hwnd, interval=1.0, query=None):
        interval = float(interval)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be positive and finite")
        self._get_hwnd = get_hwnd
        self._interval = interval
        self._query = query_group_right if query is None else query
        self._uses_com = query is None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._reading = None
        self._last_error = None

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    def reading(self):
        with self._lock:
            return self._reading

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._reading = None
            self._last_error = None
            thread = threading.Thread(
                target=self._run, args=(self._stop,), name="TaskbarProbe", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                raise

    def stop(self, timeout=2.0):
        with self._lock:
            self._stop.set()
            self._reading = None
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            if self._thread is thread and thread is not None and not thread.is_alive():
                self._thread = None

    def _run(self, stop):
        ole32 = None
        initialized = False
        try:
            if self._uses_com:
                ole32, _oleaut32 = _libraries()
                _check(ole32.CoInitializeEx(None, COINIT_MULTITHREADED), "CoInitializeEx")
                initialized = True
            while not stop.is_set():
                try:
                    hwnd = self._get_hwnd()
                    if not hwnd:
                        raise OSError("Taskbar window is unavailable")
                    right = self._query(hwnd)
                    if isinstance(right, bool) or not isinstance(right, int) or right <= 0:
                        raise OSError("Taskbar button group has no valid right edge")
                    reading = (right, time.monotonic())
                    error = None
                except Exception as exc:
                    reading = None
                    error = str(exc) or type(exc).__name__
                with self._lock:
                    if stop.is_set():
                        break
                    self._reading = reading
                    self._last_error = error
                if stop.wait(self._interval):
                    break
        except Exception as exc:
            with self._lock:
                if not stop.is_set():
                    self._reading = None
                    self._last_error = str(exc) or type(exc).__name__
        finally:
            if initialized:
                ole32.CoUninitialize()
