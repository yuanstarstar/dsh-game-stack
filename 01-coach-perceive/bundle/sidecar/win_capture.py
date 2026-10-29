# -*- coding: utf-8 -*-
"""PrintWindow 抓窗口本身（含被遮挡的部分）。

背景：gdigrab 抓的是屏幕，DSH 一挡在游戏前面，抓到的就是 DSH。
PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT) 让窗口自己把画面画到我们的位图上，
即使被别的窗口盖住也能拿到内容；顺带只抓客户区，没有标题栏。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import Optional

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


def _enable_dpi() -> None:
    """不开 DPI 感知时客户区尺寸是缩放后的逻辑值（1302 会变成 868），抓出来是糊的。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v2
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
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
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
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL

# GDI 的句柄都是指针大小，不声明 argtypes 会被当成 int32 截断
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


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """客户区在屏幕上的位置 + 尺寸 (x, y, w, h)。"""
    rc = wt.RECT()
    if not user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(rc)):
        raise RuntimeError("GetClientRect 失败")
    w, h = rc.right - rc.left, rc.bottom - rc.top
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt))
    return (pt.x, pt.y, max(1, w), max(1, h))


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rc = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rc)):
        raise RuntimeError("GetWindowRect 失败")
    return (rc.left, rc.top, max(1, rc.right - rc.left), max(1, rc.bottom - rc.top))


def capture_client(hwnd: int, timeout_flags: tuple[int, ...] = (PW_CLIENTONLY | PW_RENDERFULLCONTENT, PW_RENDERFULLCONTENT)) -> np.ndarray:
    """抓客户区，返回 BGR ndarray。兼容性不够时自动退到整窗再裁。"""
    if not user32.IsWindow(wt.HWND(hwnd)):
        raise RuntimeError("窗口句柄已失效")
    cx, cy, cw, ch = client_rect(hwnd)
    wx, wy, ww, wh = window_rect(hwnd)
    # 有些游戏 PrintWindow 只对整窗有效，所以两种都试
    for flags in timeout_flags:
        img = _print_window(hwnd, ww, wh, flags)
        if img is None:
            continue
        if flags & PW_CLIENTONLY:
            return img
        # 整窗图里裁出客户区
        ox, oy = max(0, cx - wx), max(0, cy - wy)
        crop = img[oy:oy + ch, ox:ox + cw]
        if crop.size and not _is_blank(crop):
            return crop
    raise RuntimeError("PrintWindow 抓帧失败（窗口可能不支持）")


def _is_blank(img: np.ndarray) -> bool:
    """全黑/全同色基本等于没画出来。"""
    if img.size == 0:
        return True
    return float(img.std()) < 1.5


def _print_window(hwnd: int, w: int, h: int, flags: int) -> Optional[np.ndarray]:
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
        ok = user32.PrintWindow(wt.HWND(hwnd), hdc_mem, flags)
        if not ok:
            return None
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(pbits.value)
        arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, w, 4)
        return arr[:, :, :3].copy()      # BGRA → BGR
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(wt.HWND(hwnd), hdc_screen)


def top_window_at(x: int, y: int) -> int:
    """屏幕某点最上层的窗口句柄（用来判断锚定窗口是不是被别的窗口盖住了）。"""
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.WindowFromPoint.restype = wt.HWND
    hwnd = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
    return int(hwnd or 0)


def root_owner(hwnd: int) -> int:
    """顺着 owner 链找到顶层窗口，子窗口/工具窗都能归到它的主窗。"""
    user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user32.GetAncestor.restype = wt.HWND
    try:
        root = user32.GetAncestor(wt.HWND(hwnd), 2)  # GA_ROOT
        if root:
            hwnd = int(root)
    except Exception:  # noqa: BLE001
        pass
    return int(hwnd)


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


def who_covers(hwnd: int, rect=None) -> Optional[dict]:
    """锚定窗口没有被遮挡时返回 None；被遮挡返回 {hwnd,title,at:[x,y]}。"""
    x, y, w, h = rect or window_rect(hwnd)
    px, py = int(x + w / 2), int(y + h / 4)
    top = root_owner(top_window_at(px, py))
    if not top:
        return None
    mine = root_owner(hwnd)
    if top == mine:
        return None
    if not user32.IsWindowVisible(wt.HWND(top)):
        return None
    return {"hwnd": top, "title": window_title(top), "at": [px, py]}


if __name__ == "__main__":
    import sys
    hwnd = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if not hwnd:
        raise SystemExit("用法: python win_capture.py <hwnd> [out.png]")
    from PIL import Image
    img = capture_client(hwnd)
    print({"hwnd": hwnd, "shape": list(img.shape), "std": round(float(img.std()), 2)})
    if len(sys.argv) > 2:
        Image.fromarray(img[:, :, ::-1]).save(sys.argv[2])
        print("saved", sys.argv[2])
