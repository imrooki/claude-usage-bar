# -*- coding: utf-8 -*-
"""Win32 薄封装：常量、结构体、回调类型与 Win32 类。

从 usage_widget.py 拆出的模块之一，行为与原单文件实现一致。测试对 usage_widget
打的补丁经 _entry() 取回入口模块，所以这里不直接引用被测试替换的名字。
"""

import ctypes
import ctypes.wintypes as wintypes
import sys

from usage_bar.data import SKIN_LABELS, SKIN_MECHA, SKIN_NEON


def _entry():
    """入口模块（usage_widget 或 __main__）：测试对入口模块打的补丁必须在这里仍然可见。"""
    return sys.modules.get("usage_widget") or sys.modules["__main__"]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MUTEX_NAME = "Local\\ClaudeUsageWidget"
WINDOW_CLASS = "ClaudeUsageWidgetWnd"
WINDOW_TITLE = "Claude usage"
ERROR_ALREADY_EXISTS = 183

MAX_TIMER_MS = 0x7FFFFFFF

SCALE_MIN = 0.5
SCALE_MAX = 6.0

MENU_REFRESH = 1
MENU_SNAP = 2
MENU_QUIT = 3
MENU_SKIN_NEON = 4
MENU_SKIN_MECHA = 5
# 皮肤子菜单里的勾选项：(皮肤名, 命令号)。皮肤名取自数据层的 SKINS。
SKIN_MENU_ITEMS = ((SKIN_NEON, MENU_SKIN_NEON), (SKIN_MECHA, MENU_SKIN_MECHA))
# 子菜单在 MENU_ITEMS 里的占位命令号：TrackPopupMenu 永远返回不了它。
MENU_SKIN_SUBMENU = -1
MENU_ITEMS = (
    (MENU_REFRESH, "Refresh now"),
    (MENU_SNAP, "Snap to tray"),
    (0, None),
    (MENU_SKIN_SUBMENU, "Progress Bar Skin"),
    (0, None),
    (MENU_QUIT, "Quit"),
)

# Win32 常量
# Win32 常量
ABM_GETSTATE = 4
ABM_GETTASKBARPOS = 5
ABS_AUTOHIDE = 1
ABE_TOP = 1
ABE_BOTTOM = 3
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x8
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED = 0x80000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
GW_HWNDPREV = 3
GW_OWNER = 4
GWL_EXSTYLE = -20
MAX_Z_ORDER_WINDOWS = 1024
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
OBJID_WINDOW = 0
OBJID_CLIENT = -4
CHILDID_SELF = 0
WINEVENT_OUTOFCONTEXT = 0
WINEVENT_SKIPOWNPROCESS = 2
SWP_NOSIZE = 0x1
SWP_NOMOVE = 0x2
SWP_NOZORDER = 0x4
SWP_NOACTIVATE = 0x10
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
DIB_RGB_COLORS = 0
BI_RGB = 0
IDC_ARROW = 32512
MF_STRING = 0
MF_SEPARATOR = 0x800
MF_CHECKED = 0x8
MF_POPUP = 0x10
SPI_GETCLIENTAREAANIMATION = 0x1042
TPM_RIGHTBUTTON = 0x2
TPM_NONOTIFY = 0x80
TPM_RETURNCMD = 0x100
DPI_CONTEXT_PER_MONITOR_V2 = -4
MONITOR_DEFAULTTONULL = 0
BUSY_NOTIFICATION_STATE = 2
HIDE_NOTIFICATION_STATES = frozenset({3, 4})
DWMWA_CLOAKED = 14
SUPPORTED_EDGES = frozenset({ABE_TOP, ABE_BOTTOM})
TOPMOST_SKIP_CLASSES = frozenset({
    "Windows.UI.Core.CoreWindow",
    "Xaml_WindowedPopupClass",
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
    "XamlExplorerHostIslandWindow",
    "#32768",
})
# 任务栏与桌面（含铺满多屏的 WorkerW）的矩形恰好等于任务栏或覆盖显示器；
# 用户点任务栏或桌面后它们也在顶层窗口列表里，只按矩形判断会把它们误当成全屏。
SHELL_SURFACE_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
})

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_SETTINGCHANGE = 0x001A
WM_MOUSEACTIVATE = 0x0021
WM_DISPLAYCHANGE = 0x007E
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CAPTURECHANGED = 0x0215
WM_DPICHANGED = 0x02E0
WM_APP_Z_ORDER = 0x8001
WM_APP_RECREATE = 0x8002
MA_NOACTIVATE = 3
MK_LBUTTON = 0x0001
SYSTEM_CHANGE_MESSAGES = frozenset({WM_DPICHANGED, WM_DISPLAYCHANGE, WM_SETTINGCHANGE})


# ---------------------------------------------------------------------------
# Win32 薄封装（ctypes，只用白名单里的函数）
# ---------------------------------------------------------------------------

class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", ctypes.c_ssize_t),
    ]


# rcMonitor 是整块显示器（含任务栏占用的部分），rcWork 已扣掉任务栏；
# 调用 GetMonitorInfoW 前必须先把 cbSize 设为结构体大小。
class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


# 64 位下 LRESULT 与 LPARAM 是指针宽度的有符号数，WPARAM 是指针宽度的无符号数，
# 句柄一律 c_void_p；位宽写错会在 64 位上悄悄截断。
# 回调用 Windows 的 stdcall 约定；非 Windows（离线测试）没有它，退回 CFUNCTYPE，参数表相同。
_CALLBACK_TYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDPROC = _CALLBACK_TYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
WINEVENTPROC = _CALLBACK_TYPE(
    None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
    ctypes.c_long, ctypes.c_long, ctypes.c_uint, ctypes.c_uint)
# EnumWindows 回调：返回 0 会中止枚举，所以固定返回 1 收满全部句柄。
WNDENUMPROC = _CALLBACK_TYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


def _signature(func, argtypes, restype):
    """给 ctypes 函数设好 argtypes 与 restype（64 位下不设会按 32 位 int 截断指针）。"""
    func.argtypes = argtypes
    func.restype = restype


class Win32:
    """所有 ctypes 调用集中在这里；方法名都是动作，便于测试时替换。

    对其它进程窗口的操作只有只读查询：前台窗口的类名、矩形与所属进程号，主任务栏
    及其通知区域子窗口的矩形与可见性，任务栏所在显示器的矩形。小窗以主任务栏为
    所有者；跨线程所有权会连接输入队列，主线程不能阻塞。显示、移动、置前台只针对
    本进程窗口，消息只发给本进程窗口或消息循环线程。
    """

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32")
        self.shell32 = ctypes.WinDLL("shell32")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.shcore = ctypes.WinDLL("shcore")
        except OSError:
            self.shcore = None
        try:
            self.dwmapi = ctypes.WinDLL("dwmapi")
        except OSError:
            self.dwmapi = None
        self.hinstance = None
        self._wndclass = None
        self._class_registered = False
        self._class_buffer = ctypes.create_unicode_buffer(256)
        self._event_callback = None
        self._event_hooks = []
        self._thread_id = 0
        self._declare()

    def _declare(self):
        ptr, i32, u32 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
        wstr = ctypes.c_wchar_p

        _signature(self.user32.RegisterClassExW, [ctypes.POINTER(WNDCLASSEXW)], ctypes.c_ushort)
        _signature(self.user32.UnregisterClassW, [wstr, ptr], i32)
        _signature(self.user32.CreateWindowExW, [u32, wstr, wstr, u32, i32, i32, i32, i32, ptr, ptr, ptr, ptr], ptr)
        _signature(self.user32.DestroyWindow, [ptr], i32)
        _signature(self.user32.DefWindowProcW, [ptr, u32, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t)
        # GetMessageW 出错时返回 -1，所以 restype 必须是有符号 int，循环条件写成 > 0
        _signature(self.user32.GetMessageW, [ctypes.POINTER(wintypes.MSG), ptr, u32, u32], i32)
        _signature(self.user32.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], i32)
        _signature(self.user32.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t)
        _signature(self.user32.PostQuitMessage, [i32], None)
        _signature(self.user32.PostMessageW, [ptr, u32, ctypes.c_size_t, ctypes.c_ssize_t], i32)
        _signature(self.user32.PostThreadMessageW, [wintypes.DWORD, u32, ctypes.c_size_t, ctypes.c_ssize_t], i32)
        _signature(self.user32.SetTimer, [ptr, ctypes.c_size_t, u32, ptr], ctypes.c_size_t)
        _signature(self.user32.KillTimer, [ptr, ctypes.c_size_t], i32)
        _signature(self.user32.ShowWindow, [ptr, i32], i32)
        _signature(self.user32.SetWindowPos, [ptr, ctypes.c_ssize_t, i32, i32, i32, i32, u32], i32)
        _signature(self.user32.UpdateLayeredWindow,
            [ptr, ptr, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), ptr,
             ctypes.POINTER(wintypes.POINT), u32, ctypes.POINTER(BLENDFUNCTION), u32], i32)
        _signature(self.user32.GetDC, [ptr], ptr)
        _signature(self.user32.ReleaseDC, [ptr, ptr], i32)
        _signature(self.user32.LoadCursorW, [ptr, ptr], ptr)
        _signature(self.user32.SetCapture, [ptr], ptr)
        _signature(self.user32.ReleaseCapture, [], i32)
        _signature(self.user32.GetCursorPos, [ctypes.POINTER(wintypes.POINT)], i32)
        _signature(self.user32.CreatePopupMenu, [], ptr)
        _signature(self.user32.AppendMenuW, [ptr, u32, ctypes.c_size_t, wstr], i32)
        _signature(self.user32.TrackPopupMenu, [ptr, u32, i32, i32, i32, ptr, ptr], i32)
        _signature(self.user32.DestroyMenu, [ptr], i32)
        _signature(self.user32.SetForegroundWindow, [ptr], i32)
        _signature(self.user32.SystemParametersInfoW, [u32, u32, ctypes.c_void_p, u32], i32)
        _signature(self.user32.RegisterWindowMessageW, [wstr], u32)
        _signature(self.user32.GetForegroundWindow, [], ptr)
        _signature(self.user32.GetDesktopWindow, [], ptr)
        _signature(self.user32.GetWindow, [ptr, u32], ptr)
        _signature(self.user32.SetWinEventHook, [u32, u32, ptr, WINEVENTPROC, u32, u32, u32], ptr)
        _signature(self.user32.UnhookWinEvent, [ptr], i32)
        _signature(self.user32.GetClassNameW, [ptr, wintypes.LPWSTR, i32], i32)
        _signature(self.user32.FindWindowW, [wstr, wstr], ptr)
        _signature(self.user32.FindWindowExW, [ptr, ptr, wstr, wstr], ptr)
        _signature(self.user32.GetWindowRect, [ptr, ctypes.POINTER(wintypes.RECT)], i32)
        _signature(self.user32.IsWindow, [ptr], i32)
        _signature(self.user32.IsWindowVisible, [ptr], i32)
        _signature(self.user32.GetWindowThreadProcessId, [ptr, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
        _signature(self.user32.MonitorFromRect, [ctypes.POINTER(wintypes.RECT), wintypes.DWORD], ptr)
        _signature(self.user32.GetMonitorInfoW, [ptr, ctypes.POINTER(MONITORINFO)], i32)
        _signature(self.user32.GetGuiResources, [ptr, u32], u32)
        _signature(self.user32.EnumWindows, [WNDENUMPROC, ctypes.c_ssize_t], i32)
        _signature(self.user32.GetWindowLongPtrW, [ptr, i32], ctypes.c_ssize_t)
        # dwmapi 较旧系统上也可能缺失；签名建不上就按「拿不到」处理
        if self.dwmapi is not None:
            try:
                _signature(self.dwmapi.DwmGetWindowAttribute,
                           [ptr, wintypes.DWORD, ptr, wintypes.DWORD], ctypes.c_long)
            except AttributeError:
                self.dwmapi = None

        _signature(self.gdi32.CreateCompatibleDC, [ptr], ptr)
        _signature(self.gdi32.DeleteDC, [ptr], i32)
        _signature(self.gdi32.CreateDIBSection,
            [ptr, ctypes.POINTER(BITMAPINFOHEADER), u32, ctypes.POINTER(ptr), ptr, u32], ptr)
        _signature(self.gdi32.SelectObject, [ptr, ptr], ptr)
        _signature(self.gdi32.DeleteObject, [ptr], i32)

        _signature(self.shell32.SHAppBarMessage, [wintypes.DWORD, ctypes.POINTER(APPBARDATA)], ctypes.c_size_t)
        _signature(self.shell32.SHQueryUserNotificationState, [ctypes.POINTER(i32)], ctypes.c_long)

        _signature(self.kernel32.CreateMutexW, [ptr, i32, wstr], ptr)
        _signature(self.kernel32.CloseHandle, [ptr], i32)
        _signature(self.kernel32.GetCurrentProcess, [], ptr)
        _signature(self.kernel32.GetCurrentProcessId, [], wintypes.DWORD)
        _signature(self.kernel32.GetCurrentThreadId, [], wintypes.DWORD)
        _signature(self.kernel32.GetModuleHandleW, [wstr], ptr)

        # DPI 相关函数较新，旧系统上可能没有，缺失时在调用处按"拿不到"处理
        try:
            _signature(self.user32.SetProcessDpiAwarenessContext, [ctypes.c_ssize_t], i32)
            _signature(self.user32.GetDpiForWindow, [ptr], u32)
            _signature(self.user32.GetDpiForSystem, [], u32)
        except AttributeError:
            pass
        if self.shcore is not None:
            try:
                _signature(self.shcore.SetProcessDpiAwareness, [i32], ctypes.c_long)
            except AttributeError:
                self.shcore = None

    # DPI -----------------------------------------------------------------

    def set_dpi_awareness(self):
        """创建窗口之前调用：先试每显示器 V2，再试 shcore，都失败就忽略。"""
        try:
            if self.user32.SetProcessDpiAwarenessContext(DPI_CONTEXT_PER_MONITOR_V2):
                return "per-monitor-v2"
        except (AttributeError, OSError):
            pass
        if self.shcore is not None:
            try:
                if self.shcore.SetProcessDpiAwareness(2) == 0:
                    return "per-monitor"
            except OSError:
                pass
        return "unchanged"

    def scale_for(self, hwnd):
        """GetDpiForWindow / 96；拿不到就 GetDpiForSystem / 96，再不行 1.0。"""
        dpi = 0
        if hwnd:
            try:
                dpi = self.user32.GetDpiForWindow(hwnd)
            except (AttributeError, OSError):
                dpi = 0
        if not dpi:
            try:
                dpi = self.user32.GetDpiForSystem()
            except (AttributeError, OSError):
                dpi = 0
        if not dpi:
            return 1.0
        return max(SCALE_MIN, min(SCALE_MAX, dpi / 96.0))

    # 窗口类、窗口与消息循环 ------------------------------------------------

    def register_class(self, wndproc):
        self._thread_id = self.kernel32.GetCurrentThreadId()
        self.hinstance = self.kernel32.GetModuleHandleW(None)
        wndclass = WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wndclass.style = 0
        wndclass.lpfnWndProc = wndproc
        wndclass.hInstance = self.hinstance
        wndclass.hCursor = self.user32.LoadCursorW(None, IDC_ARROW)
        wndclass.lpszClassName = _entry().WINDOW_CLASS
        if not self.user32.RegisterClassExW(ctypes.byref(wndclass)):
            raise OSError("RegisterClassExW failed, error %d" % ctypes.get_last_error())
        self._wndclass = wndclass
        self._class_registered = True

    def unregister_class(self):
        if self._class_registered:
            self._class_registered = False
            self.user32.UnregisterClassW(_entry().WINDOW_CLASS, self.hinstance)

    def create_window(self, x, y, width, height, owner=None):
        """无父窗口的顶层分层窗口，创建时不可见。

        owner 可指定主任务栏；系统抬升任务栏层级时（如打开开始菜单），小窗随之抬升。
        """
        ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        hwnd = self.user32.CreateWindowExW(
            ex_style, _entry().WINDOW_CLASS, WINDOW_TITLE, WS_POPUP, x, y, width, height,
            owner or None, None, self.hinstance, None)
        if not hwnd:
            raise OSError("CreateWindowExW failed, error %d" % ctypes.get_last_error())
        return hwnd

    def destroy_window(self, hwnd):
        return bool(self.user32.DestroyWindow(hwnd))

    def post_quit(self):
        self.user32.PostQuitMessage(0)

    def post_thread_message(self, message):
        return bool(self.user32.PostThreadMessageW(self._thread_id, message, 0, 0))

    def def_window_proc(self, hwnd, msg, wparam, lparam):
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run_message_loop(self, thread_handler=None, error_handler=None):
        """返回 0 表示收到 WM_QUIT；-1 表示 GetMessageW 出错。"""
        msg = wintypes.MSG()
        result = self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        while result > 0:
            if not msg.hWnd and msg.message == WM_APP_RECREATE:
                if thread_handler is not None:
                    try:
                        thread_handler(msg.message)
                    except Exception as exc:
                        if error_handler is not None:
                            error_handler(exc)
                        else:
                            _entry().say("thread message handler failed: %s: %s" % (type(exc).__name__, exc))
            else:
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))
            result = self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        return result

    def register_message(self, name):
        return int(self.user32.RegisterWindowMessageW(name))

    def watch_window_events(self, handler):
        """进程外只读事件通知；回调只投递消息，不在回调里改变窗口层级。

        覆盖前台切换、外壳重排与普通窗口的尺寸/位置变化——全屏进出主要靠最后一个。
        """
        def callback(_hook, event, hwnd, object_id, child_id, _thread, _time):
            try:
                handler(event, hwnd or 0, object_id, child_id)
            except BaseException:
                # ctypes 回调不能让异常逃到系统。
                pass

        self._event_callback = WINEVENTPROC(callback)
        try:
            for event in (
                    EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_REORDER,
                    EVENT_OBJECT_LOCATIONCHANGE):
                hook = self.user32.SetWinEventHook(
                    event, event, None, self._event_callback, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
                if not hook:
                    raise OSError("SetWinEventHook failed for event %d" % event)
                self._event_hooks.append(hook)
        except BaseException:
            self.unwatch_window_events()
            raise

    def unwatch_window_events(self):
        """由注册通知的主线程注销；失败的句柄和回调保留，允许收尾时重试。"""
        remaining = []
        for hook in self._event_hooks:
            if not self.user32.UnhookWinEvent(hook):
                remaining.append(hook)
        self._event_hooks = remaining
        if not remaining:
            self._event_callback = None

    def post_z_order_check(self, hwnd):
        return bool(self.user32.PostMessageW(hwnd, WM_APP_Z_ORDER, 0, 0))

    def set_timer(self, hwnd, timer_id, interval_ms):
        interval_ms = max(10, min(int(interval_ms), MAX_TIMER_MS))
        return bool(self.user32.SetTimer(hwnd, timer_id, interval_ms, None))

    def kill_timer(self, hwnd, timer_id):
        return bool(self.user32.KillTimer(hwnd, timer_id))

    def show_window(self, hwnd, command):
        self.user32.ShowWindow(hwnd, command)

    def move_window(self, hwnd, x, y):
        """只移动：不改尺寸、不动 Z 序、不激活。"""
        return bool(self.user32.SetWindowPos(
            hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE))

    def keep_topmost(self, hwnd):
        return bool(self.user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))

    def update_layered(self, hwnd, x, y, width, height, bgra):
        """把预乘 alpha 的 BGRA 位图提交给分层窗口，同时设定位置与尺寸。

        每次提交都新建 DIB 与内存 DC，并在 finally 里按相反顺序全部释放，GDI 对象
        数量不随提交次数增长。
        """
        if len(bgra) != width * height * 4:
            raise ValueError("bitmap is %d bytes, expected %d" % (len(bgra), width * height * 4))
        hdc_screen = self.user32.GetDC(None)
        hdc_mem = None
        hbitmap = None
        old_bitmap = None
        try:
            hdc_mem = self.gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                raise OSError("CreateCompatibleDC failed")
            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height  # 负数：自顶向下，第一行就是图像的最上一行
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            hbitmap = self.gdi32.CreateDIBSection(
                hdc_screen, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not hbitmap or not bits.value:
                raise OSError("CreateDIBSection failed")
            old_bitmap = self.gdi32.SelectObject(hdc_mem, hbitmap)
            ctypes.memmove(bits.value, bgra, len(bgra))
            destination = wintypes.POINT(x, y)
            size = wintypes.SIZE(width, height)
            source = wintypes.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            if not self.user32.UpdateLayeredWindow(
                    hwnd, hdc_screen, ctypes.byref(destination), ctypes.byref(size), hdc_mem,
                    ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA):
                raise OSError("UpdateLayeredWindow failed, error %d" % ctypes.get_last_error())
        finally:
            if hdc_mem and old_bitmap:
                self.gdi32.SelectObject(hdc_mem, old_bitmap)
            if hbitmap:
                self.gdi32.DeleteObject(hbitmap)
            if hdc_mem:
                self.gdi32.DeleteDC(hdc_mem)
            if hdc_screen:
                self.user32.ReleaseDC(None, hdc_screen)

    # 鼠标与菜单 ----------------------------------------------------------

    def cursor_pos(self):
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos failed, error %d" % ctypes.get_last_error())
        return point.x, point.y

    def set_capture(self, hwnd):
        self.user32.SetCapture(hwnd)

    def release_capture(self):
        self.user32.ReleaseCapture()

    def animations_enabled(self):
        """系统是否允许客户端区域动画（SPI_GETCLIENTAREAANIMATION）；查询失败按允许。

        返回真正的 bool：测试里的假 w32（Mock）拿不到 True，动画就不会开。
        """
        value = ctypes.c_int(1)
        ok = self.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0)
        if not ok:
            return True
        return bool(value.value)

    def track_menu(self, hwnd, x, y, skin=None):
        """弹出右键菜单，返回选中的命令号（取消返回 0）。

        第三个菜单项是 "Progress Bar Skin" 子菜单，列出全部皮肤，当前皮肤打勾（skin
        为 None 时都不打勾）。TrackPopupMenu 自带模态消息循环，期间计时器消息照常送到
        窗口过程。弹出前把自己的窗口设为前台，否则点菜单以外的地方菜单不会收起；
        系统可能拒绝这次设置，不影响菜单本身，所以不检查返回值。
        """
        menu = self.user32.CreatePopupMenu()
        if not menu:
            raise OSError("CreatePopupMenu failed, error %d" % ctypes.get_last_error())
        try:
            for item_id, text in MENU_ITEMS:
                if text is None:
                    self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                elif item_id == MENU_SKIN_SUBMENU:
                    submenu = self.user32.CreatePopupMenu()
                    if not submenu:
                        raise OSError("CreatePopupMenu failed, error %d" % ctypes.get_last_error())
                    for skin_name, command in SKIN_MENU_ITEMS:
                        flags = MF_STRING | (MF_CHECKED if skin_name == skin else 0)
                        self.user32.AppendMenuW(submenu, flags, command, SKIN_LABELS[skin_name])
                    self.user32.AppendMenuW(menu, MF_POPUP, submenu, text)
                else:
                    self.user32.AppendMenuW(menu, MF_STRING, item_id, text)
            self.user32.SetForegroundWindow(hwnd)
            command = self.user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, x, y, 0, hwnd, None)
            self.user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            # 子菜单随父菜单一起销毁，不再单独 DestroyMenu。
            self.user32.DestroyMenu(menu)
        return int(command)

    # 只读查询 ------------------------------------------------------------

    def foreground_hwnd(self):
        return self.user32.GetForegroundWindow() or 0

    def desktop_hwnd(self):
        return self.user32.GetDesktopWindow() or 0

    def taskbar_hwnd(self):
        return self.user32.FindWindowW("Shell_TrayWnd", None) or 0

    def window_owner(self, hwnd):
        """只读诊断；开始菜单调整任务栏 band 时 GW_OWNER 会临时变化，不能据此重建。"""
        return self.user32.GetWindow(hwnd, GW_OWNER) or 0

    def is_window(self, hwnd):
        return bool(self.user32.IsWindow(hwnd))

    def taskbar_above(self, hwnd):
        covered, allowed = self._z_order_state(hwnd)
        return covered and allowed

    def topmost_allowed(self, hwnd):
        return self._z_order_state(hwnd)[1]

    def window_rect(self, hwnd):
        rect = wintypes.RECT()
        if not hwnd or not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def window_pid(self, hwnd):
        """窗口所属进程号；句柄为空或窗口已销毁返回 0。"""
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        # 返回值是线程号，0 表示失败，此时 pid 的内容不可信
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return 0
        return int(pid.value)

    def monitor_rect_for(self, rect):
        """rect 所在显示器的完整矩形（rcMonitor）；任一步失败返回 None。

        用 MONITOR_DEFAULTTONULL：矩形不落在任何显示器上时得到 None，由调用方另想
        办法，而不是悄悄换成最近的显示器。
        """
        query = wintypes.RECT(*rect)
        monitor = self.user32.MonitorFromRect(ctypes.byref(query), MONITOR_DEFAULTTONULL)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        area = info.rcMonitor
        return area.left, area.top, area.right, area.bottom

    def top_level_windows(self):
        """当前所有顶层窗口句柄，顺序即 EnumWindows 的枚举顺序。"""
        handles = []

        def collect(hwnd, _lparam):
            handles.append(hwnd)
            return 1

        callback = WNDENUMPROC(collect)
        self.user32.EnumWindows(callback, 0)
        return handles

    def window_cloaked(self, hwnd):
        """窗口是否被 DWM 隐藏（挂起的 UWP 等）；查询不可用时按未隐藏处理。"""
        if self.dwmapi is None:
            return False
        value = wintypes.DWORD(0)
        result = self.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        if result != 0:
            return False
        return bool(value.value)

    def fullscreen_window_present(self):
        """是否有任一普通顶层窗口铺满其所在显示器，即全屏应用。

        与前台焦点无关：全屏应用被切到后台后仍算。下列窗口都不算：不可见窗口、
        工具窗、被 DWM 隐藏的窗口、本进程自己的窗口、任务栏与桌面外壳类、拿不到
        矩形或所在显示器的窗口；最大化窗口盖不满显示器，天然不算。
        """
        own_pid = self.kernel32.GetCurrentProcessId()
        for hwnd in self.top_level_windows():
            if not self.user32.IsWindowVisible(hwnd):
                continue
            if self.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                continue
            if self.window_cloaked(hwnd):
                continue
            if self.window_pid(hwnd) == own_pid:
                continue
            if self.class_name(hwnd) in SHELL_SURFACE_CLASSES:
                continue
            window = self.window_rect(hwnd)
            if window is None:
                continue
            monitor = self.monitor_rect_for(window)
            if monitor is None:
                continue
            if _entry().rect_inside(monitor, window):
                return True
        return False

    def shell_popup_overlaps(self, popup, hwnd):
        """只避让真正覆盖小窗的可见系统面板，不因面板打开就放弃层级。"""
        if (not self.user32.IsWindowVisible(popup)
                or self.class_name(popup) not in TOPMOST_SKIP_CLASSES):
            return False
        popup_rect = self.window_rect(popup)
        own_rect = self.window_rect(hwnd)
        # 拿不到矩形时保守避让；下次事件或两秒检查会再确认。
        return popup_rect is None or own_rect is None or _entry().rects_overlap(popup_rect, own_rect)

    def _z_order_state(self, hwnd):
        """只读返回 (任务栏在上方, 允许恢复层级)，有界遍历以容忍重排竞争。"""
        tray = self.user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return False, False
        seen = {hwnd}
        covered = False
        previous = self.user32.GetWindow(hwnd, GW_HWNDPREV)
        for _index in range(MAX_Z_ORDER_WINDOWS):
            if not previous:
                return covered, True
            if previous in seen:
                return False, False
            if previous == tray:
                covered = True
            elif self.shell_popup_overlaps(previous, hwnd):
                return covered, False
            seen.add(previous)
            previous = self.user32.GetWindow(previous, GW_HWNDPREV)
        return False, False

    def class_name(self, hwnd):
        if not hwnd:
            return ""
        length = self.user32.GetClassNameW(hwnd, self._class_buffer, 256)
        return self._class_buffer.value if length > 0 else ""

    def tray_notify_rect(self):
        """主任务栏的通知区域（TrayNotifyWnd）矩形；找不到或不可见返回 None。

        只读：先找主任务栏，再在它下面找通知区域子窗口，读可见性与矩形。资源管理器
        重启后句柄会变，所以每次都重新查找。
        """
        tray = self.user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        notify = self.user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
        if not notify or not self.user32.IsWindowVisible(notify):
            return None
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(notify, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def taskbar_pos(self):
        """返回 ((left, top, right, bottom), edge)，失败返回 None。"""
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        if not self.shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
            return None
        rect = data.rc
        return (rect.left, rect.top, rect.right, rect.bottom), int(data.uEdge)

    def taskbar_autohide(self):
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        state = self.shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data))
        return bool(state & ABS_AUTOHIDE)

    def notification_state(self):
        state = ctypes.c_int(0)
        if self.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return None
        return state.value

    # 进程 ----------------------------------------------------------------

    def create_mutex(self, name):
        """返回 (句柄或 0, GetLastError)。last error 必须紧跟在调用之后取。"""
        handle = self.kernel32.CreateMutexW(None, False, name)
        return handle or 0, ctypes.get_last_error()

    def close_handle(self, handle):
        if handle:
            self.kernel32.CloseHandle(handle)

    def gui_resources(self, flag):
        """flag 0 为 GDI 对象数，1 为 USER 对象数。"""
        return int(self.user32.GetGuiResources(self.kernel32.GetCurrentProcess(), flag))

