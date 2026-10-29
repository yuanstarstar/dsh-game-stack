# -*- coding: utf-8 -*-
"""帧登记表 + 目标解析 + 动作守卫。

为什么要有"帧"这个东西（而不是让模型直接给坐标）：

* 模型给的坐标是**上一眼**看到的；从"看到"到"动手"之间画面可能已经变了、窗口可能被
  拖走了、DPI 可能不同。所以动作不认裸坐标，只认「哪一帧里的哪个目标」；
* 目标用 **id**（实体 `heart_2`、文字块 `t3`）或**文字**（`继续`）表示，模型不做算术；
* 真正下发前现算屏幕坐标（`wincap.client_to_screen`），并在需要时用感知包把它重新
  定位一次——位移、缩放都不影响。

坐标口径（写死，全链路一致）：
* 帧内坐标 = **客户区像素**（左上角为原点，跟 `capture_client` 出来的图一致）；
* 屏幕坐标 = 客户区坐标 + 窗口客户区原点（每次现算）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../sidecar
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wincap  # noqa: E402  （sidecar 目录在 sys.path 上，cvcd 也是这么导的）

from . import feats  # noqa: E402

TTL_MS = 2500
MAX_FRAMES = 8
THUMB = (160, 90)
GRID = (8, 5)                     # 回执里"变了几格"用的格子数

FRAMES: dict = {}
_SEQ = [0]


def _thumb(rgb):
    small = feats.resize_area(feats.to_gray(np.asarray(rgb)), THUMB[0], THUMB[1])
    return small.astype(np.float32)


def new(hwnd: int, rgb, pack_id=None, boxes=None, texts=None, scene=None, labels=None):
    """登记一帧，返回记录（里面带一个短 id，如 `f17`）。"""
    _SEQ[0] += 1
    fid = "f%d" % _SEQ[0]
    arr = np.asarray(rgb)
    rec = {
        "id": fid,
        "hwnd": int(hwnd),
        "w": int(arr.shape[1]),
        "h": int(arr.shape[0]),
        "born": time.time() * 1000.0,
        "pack": pack_id,
        "scene": scene,
        "boxes": {k: [int(v) for v in b] for k, b in (boxes or {}).items() if b and len(b) == 4},
        "labels": dict(labels or {}),
        "texts": [dict(t) for t in (texts or []) if t],
        "thumb": _thumb(arr),
    }
    FRAMES[fid] = rec
    for old in sorted(FRAMES, key=lambda k: FRAMES[k]["born"])[:-MAX_FRAMES]:
        FRAMES.pop(old, None)
    return rec


def get(fid: str):
    rec = FRAMES.get(str(fid or ""))
    if rec is None:
        raise KeyError(f"没有帧「{fid}」；先调 cv_recall 拿一个帧号（最近的是 "
                       f"{max(FRAMES, key=lambda k: FRAMES[k]['born']) if FRAMES else '无'}）")
    return rec


def latest(hwnd=None):
    cand = [r for r in FRAMES.values() if hwnd is None or r["hwnd"] == int(hwnd)]
    if not cand:
        return None
    return max(cand, key=lambda r: r["born"])


def age_ms(rec) -> int:
    return int(time.time() * 1000.0 - rec["born"])


# ---------------------------------------------------------------- 目标解析
def _text_hit(rec, needle: str):
    key = str(needle or "").strip().lower()
    if not key:
        return None
    # 先精确、再包含；包含时取最短的那块（"继续"不该命中"继续游戏（推荐）"以外的长句）
    exact = [t for t in rec["texts"] if str(t.get("text", "")).strip().lower() == key]
    if exact:
        return exact[0]
    loose = [t for t in rec["texts"] if key in str(t.get("text", "")).strip().lower()]
    if not loose:
        return None
    return min(loose, key=lambda t: len(str(t.get("text", ""))))


_COORD_RE = re.compile(
    r"^\s*[\(（\[]?\s*(-?\d+(?:\.\d+)?)\s*[,，;；\s]\s*(-?\d+(?:\.\d+)?)\s*[\)）\]]?\s*$")


def _maybe_point(target):
    """把「看着像坐标的字符串」变成 `{x,y}`。

    模型有时会把 `click: {x,y}` 序列化成字符串传进来（排障报告里出现过：
    帧里查不到「{"x": 509, "y": 187}」这种 id）。坐标就是坐标，不该因为多了一层
    引号就变成"查无此物"，所以在这里统一认一次。
    """
    s = str(target).strip()
    if not s:
        return None
    if s[0] in "{[":
        try:
            v = json.loads(s)
        except Exception:  # noqa: BLE001  不是 JSON 就当普通 id/文字
            v = None
        if isinstance(v, dict):
            return v
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return {"x": v[0], "y": v[1]}
    m = _COORD_RE.match(s)
    if m:
        return {"x": float(m.group(1)), "y": float(m.group(2))}
    return None


def resolve_target(rec, target):
    """把"点什么"解析成帧内的框。返回 (box[4], how, note)。

    `target` 可以是：
    * `"t3"` / `"heart_2"` —— 帧里登记过的 id；
    * `"继续"` —— 按文字找那一块（也支持实体 label）；
    * `{"x":120,"y":340}` —— 裸坐标：**按 coordSpace 解释**，默认屏幕绝对坐标（与 coach_* 一致），
      `coordSpace:"client"` 时才是画面坐标；换算在 cvcd._point_of 里做；
    * `{"screen":[x,y]}` —— 明确按屏幕绝对坐标（不受 coordSpace 影响）。
    """
    if isinstance(target, str):
        cand = _maybe_point(target)
        if isinstance(cand, dict):
            target = cand

    if isinstance(target, dict):
        if "screen" in target:
            sx, sy = float(target["screen"][0]), float(target["screen"][1])
            ox, oy, _, _ = wincap.client_rect(rec["hwnd"])
            return [int(sx - ox), int(sy - oy), 1, 1], "screen", "屏幕绝对坐标"
        if "x" in target and "y" in target:
            return [int(target["x"]), int(target["y"]), 1, 1], "point", "客户区坐标"
        if "id" in target:
            return resolve_target(rec, target["id"])
        if "text" in target:
            return resolve_target(rec, target["text"])
        raise ValueError("target 要写成 id/文字字符串，或 {x,y}/{screen:[x,y]}/{id:..}/{text:..}")

    key = str(target or "").strip()
    if not key:
        raise ValueError("没给要点哪儿：给 id（如 t3）、文字（如 继续）或 {x,y}")
    if key in rec["boxes"]:
        return list(rec["boxes"][key]), "id", key
    by_id = [t for t in rec["texts"] if str(t.get("id") or "") == key]
    if by_id:                                   # 文字块 id（cv_recall 里叫 t1/t2/…）
        return [int(v) for v in by_id[0]["box"]], "text_id", by_id[0].get("text", key)
    t = _text_hit(rec, key)
    if t is not None:
        return [int(v) for v in t["box"]], "text", t.get("text", key)
    # 实体 label 也算（用户更可能说"血量"而不是 heart_2）
    for eid, lab in rec["labels"].items():
        if str(lab).strip().lower() == key.lower() and eid in rec["boxes"]:
            return list(rec["boxes"][eid]), "label", eid
    where = f"帧 {rec['id']}（窗口 {rec['hwnd']}，画面 {rec['w']}x{rec['h']}）"
    if not rec["boxes"] and not rec["texts"]:
        raise KeyError(
            f"{where} 里没有任何能指路的东西：这个窗口还没有感知包，所以既没有实体 id 也没有文字块。"
            "现在可以直接给坐标 click:{x,y}；想按 id/文字点，先 cv_learn{action:\"scan\"} 再 build 一个包")
    near = "、".join(list(rec["boxes"])[:6] + [str(t.get("text"))[:8] for t in rec["texts"][:6]])
    raise KeyError(f"{where} 里没有「{key}」。这一帧有的是：{near}")


# ---------------------------------------------------------------- 守卫
def _in_client(rec, x, y) -> bool:
    return 0 <= x < rec["w"] and 0 <= y < rec["h"]


def guard_point(rec, x: float, y: float, allow_windows=None, recover: bool = False):
    """检查"这个点现在点下去会不会落到别人身上"。返回 (ok, code, message, recovered)。

    code 为空串表示放行。所有拒绝都给出**能直接照做**的中文原因。
    """
    if not _in_client(rec, x, y):
        return False, "OUT_OF_BOUNDS", f"点 ({int(x)},{int(y)}) 在客户区外面（画面 {rec['w']}x{rec['h']}）", False
    hwnd = rec["hwnd"]
    if not wincap.user32.IsWindow(wincap.wt.HWND(hwnd)):
        return False, "WINDOW_GONE", f"窗口 {hwnd} 已经不在了（游戏退出了或换窗口了）", False
    sx, sy = wincap.client_to_screen(hwnd, x, y)
    allow = {int(v) for v in (allow_windows or [])} | {hwnd}
    top = wincap.top_window_at(sx, sy)
    recovered = False
    if top not in allow:
        if recover:                       # 只试一次：SetForegroundWindow 有系统自己的规矩
            wincap.set_foreground(hwnd)
            top = wincap.top_window_at(sx, sy)
            recovered = True
        if top not in allow:
            who = wincap.window_title(top)[:30] if top else "（无窗口）"
            return False, "TARGET_OCCLUDED", (
                f"点 ({sx},{sy}) 被「{who}」挡住了（句柄 {top}）。"
                "先把它挪开/最小化，或改用 allow_windows 明确允许那个窗口"), recovered
    if wincap.foreground() != hwnd:
        return False, "NOT_FOREGROUND", (
            f"目标窗口不在前台（前台是 {wincap.foreground()}）。"
            "用 recover:true 让我试着切过去，或先 point 一下它"), recovered
    return True, "", "", recovered


# ---------------------------------------------------------------- 变化检测
def diff(rec, rgb) -> dict:
    """跟登记那一帧比，变了几格（0~GRID 格数）。动画面会一直变，这时只看"有没有变化"。"""
    cur = _thumb(rgb)
    th = rec["thumb"]
    if cur.shape != th.shape:
        return {"changed": int(GRID[0] * GRID[1]), "frac": 1.0, "resized": True, "boxes": []}
    d = np.abs(cur - th)
    gw, gh = GRID
    hh, ww = d.shape[0] // gh, d.shape[1] // gw
    cells = d[:hh * gh, :ww * gw].reshape(gh, hh, gw, ww).mean(axis=(1, 3))
    hit = cells > 0.012
    changed = int(hit.sum())
    # 变化的格子拼成若干矩形（客户区坐标），让回执能说"哪里变了"而不是只给个数字
    boxes = []
    if changed:
        tile_w = rec["w"] / float(gw)
        tile_h = rec["h"] / float(gh)
        for gy, gx in zip(*np.nonzero(hit)):
            boxes.append([int(round(gx * tile_w)), int(round(gy * tile_h)),
                          int(round(tile_w)), int(round(tile_h))])
            if len(boxes) >= 6:
                break
    return {"changed": changed, "frac": round(float(d.mean()), 4), "boxes": boxes}
