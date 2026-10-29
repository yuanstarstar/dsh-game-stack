# -*- coding: utf-8 -*-
"""窗口捕获 + 枚举（PrintWindow，被遮挡也能抓）。

为什么要 PrintWindow：DSH 自己是前端窗口，gdigrab 抓的是屏幕，
一挡住游戏抓到就是 DSH 自己。PrintWindow(PW_RENDERFULLCONTENT) 让窗口
自己把内容画到内存位图上，被别的窗口盖住也不影响。

输出约定：**RGB uint8**（跟 PIL / numpy 习惯一致，避免 BGR 混用出错）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


def _enable_dpi() -> None:
    """不开 DPI 感知时客户区尺寸是缩放后的逻辑值，抓出来是糊的。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


_enable_dpi()

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ClientToScreen.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL


def client_rect(hwnd: int):
    rc = wt.RECT()
    if not user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(rc)):
        raise RuntimeError("GetClientRect 失败")
    w, h = rc.right - rc.left, rc.bottom - rc.top
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt))
    return (pt.x, pt.y, max(1, w), max(1, h))


def window_rect(hwnd: int):
    rc = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rc)):
        raise RuntimeError("GetWindowRect 失败")
    return (rc.left, rc.top, max(1, rc.right - rc.left), max(1, rc.bottom - rc.top))


def _is_blank(img) -> bool:
    return img.size == 0 or float(img.std()) < 1.5


def _print_window(hwnd: int, w: int, h: int, flags: int):
    hdc_screen = user32.GetDC(wt.HWND(hwnd))
    if not hdc_screen:
        raise RuntimeError("GetDC 失败")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    if not hdc_mem:
        user32.ReleaseDC(wt.HWND(hwnd), hdc_screen)
        raise RuntimeError("CreateCompatibleDC 失败")
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负值 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    pbits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                 ctypes.byref(pbits), None, 0)
    if not hbm:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(wt.HWND(hwnd), hdc_screen)
        raise RuntimeError("CreateDIBSection 失败")
    old = gdi32.SelectObject(hdc_mem, hbm)
    try:
        if not user32.PrintWindow(wt.HWND(hwnd), hdc_mem, flags):
            return None
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(pbits.value)
        arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, w, 4)
        return arr[:, :, 2::-1].copy()   # BGRA -> RGB
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(wt.HWND(hwnd), hdc_screen)


def capture_client(hwnd: int):
    """抓客户区，返回 RGB uint8。兼容性不够时自动退到整窗再裁。"""
    if not user32.IsWindow(wt.HWND(hwnd)):
        raise RuntimeError("窗口句柄已失效")
    cx, cy, cw, ch = client_rect(hwnd)
    wx, wy, ww, wh = window_rect(hwnd)
    for flags in (PW_CLIENTONLY | PW_RENDERFULLCONTENT, PW_RENDERFULLCONTENT):
        img = _print_window(hwnd, ww, wh, flags)
        if img is None:
            continue
        if flags & PW_CLIENTONLY:
            if not _is_blank(img):
                return img
            continue
        ox, oy = max(0, cx - wx), max(0, cy - wy)
        crop = img[oy:oy + ch, ox:ox + cw]
        if crop.size and not _is_blank(crop):
            return crop
    raise RuntimeError("PrintWindow 抓帧失败（窗口可能不支持或被最小化）")


def window_title(hwnd: int) -> str:
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    n = user32.GetWindowTextLengthW(wt.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, n + 1)
    return buf.value


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def list_windows(min_size: int = 120):
    """可见且有标题的顶层窗口。返回 [{hwnd,title,rect:[x,y,w,h],fg}]。"""
    out = []
    fg = foreground()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _lparam):
        h = int(hwnd)
        if not user32.IsWindowVisible(wt.HWND(h)):
            return True
        title = window_title(h)
        if not title.strip():
            return True
        try:
            x, y, w, hh = window_rect(h)
        except Exception:  # noqa: BLE001
            return True
        if w < min_size or hh < min_size:
            return True
        out.append({"hwnd": h, "title": title, "rect": [x, y, w, hh], "fg": h == fg})
        return True

    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    out.sort(key=lambda d: (not d["fg"], d["title"].lower()))
    return out


def resolve_window(hwnd: int = 0, title: str = "") -> int:
    """按句柄或标题子串找窗口；标题优先精确、其次前台、其次第一个匹配。"""
    if hwnd:
        if not user32.IsWindow(wt.HWND(int(hwnd))):
            raise RuntimeError(f"窗口句柄 {hwnd} 已失效")
        return int(hwnd)
    if not title:
        raise RuntimeError("需要 hwnd 或 title 之一")
    wins = list_windows()
    key = title.strip().lower()
    exact = [w for w in wins if w["title"].strip().lower() == key]
    loose = [w for w in wins if key in w["title"].strip().lower()]
    pick = (exact or loose)
    if not pick:
        names = "、".join(w["title"][:24] for w in wins[:8])
        raise RuntimeError(f"没找到标题含「{title}」的窗口；当前可见窗口：{names}")
    fg = [w for w in pick if w["fg"]]
    return int((fg or pick)[0]["hwnd"])


def screen_size():
    return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))


# ---------------------------------------------------------------- 给"手"用的窗口事实
def client_to_screen(hwnd: int, x: float, y: float):
    """客户区坐标 → 屏幕绝对坐标（每次现算：窗口可能被拖走了）。"""
    ox, oy, _, _ = client_rect(hwnd)
    return (int(round(ox + x)), int(round(oy + y)))


GA_ROOT = 2


def top_window_at(x: int, y: int) -> int:
    """屏幕某点上**最前面那个顶层窗口**的句柄（用来判断"我要点的地方有没有被挡住"）。"""
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.WindowFromPoint.restype = wt.HWND
    user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user32.GetAncestor.restype = wt.HWND
    h = int(user32.WindowFromPoint(wt.POINT(int(x), int(y))) or 0)
    if not h:
        return 0
    root = int(user32.GetAncestor(wt.HWND(h), GA_ROOT) or 0)
    return root or h


def set_foreground(hwnd: int, tries: int = 3, settle_ms: int = 120) -> bool:
    """把窗口抢到前台——**抢到才算成功**，抢不到返回 False 由上层如实报错。

    顺序按实机验证过的那一套：
      ShowWindow(SW_RESTORE) → AttachThreadInput(前台线程, 本线程) →
      BringWindowToTop → SetForegroundWindow → SetActiveWindow → detach

    为什么不能只调 SetForegroundWindow：当前台属于别的进程时，系统会**静默忽略**
    这次请求（不改返回值），于是回执写着"前台是游戏"，实际点击和按键却落到了
    DSH 自己身上。这正是一整套"点了没反应"的根源。
    """
    hwnd = int(hwnd or 0)
    if not hwnd:
        return False
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wt.BOOL
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.BringWindowToTop.restype = wt.BOOL
    user32.SetActiveWindow.argtypes = [wt.HWND]
    user32.SetActiveWindow.restype = wt.HWND
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.AttachThreadInput.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentThreadId.restype = wt.DWORD
    target = wt.HWND(hwnd)
    SW_RESTORE = 9
    for _ in range(max(1, int(tries))):
        try:
            user32.ShowWindow(target, SW_RESTORE)
            fg = user32.GetForegroundWindow()
            fg_tid = int(user32.GetWindowThreadProcessId(wt.HWND(fg), None) or 0) if fg else 0
            me = int(k32.GetCurrentThreadId())
            attached = bool(user32.AttachThreadInput(fg_tid, me, True)) if fg_tid and fg_tid != me else False
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
            user32.SetActiveWindow(target)
            if attached:
                user32.AttachThreadInput(fg_tid, me, False)
        except Exception:  # noqa: BLE001
            return False
        if foreground() == hwnd:
            return True
        time.sleep(max(0, int(settle_ms)) / 1000.0)
    return foreground() == hwnd
