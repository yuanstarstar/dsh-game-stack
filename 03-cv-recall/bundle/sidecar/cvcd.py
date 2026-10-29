#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cvcd —— 感知编译器 sidecar（DSH 插件 dsh-cv-recall 的 Python 侧）。

一句话：**看一眼画面，编译成毫秒级、零 token 的本地识别器。**

启动时 stdout 首行打印握手 JSON：{"ok":true,"port":N,...}，然后 serve_forever。
每个接口都返回 JSON；失败返回 {"ok":false,"error":"..."}。

  GET  /health                       存活、包数量、数据目录
  GET  /windows                      可见窗口列表（hwnd/标题/矩形/是否前台）
  POST /scan     {hwnd|title|path}   抓一帧 + 免费候选框提议（不花 token 的“探测器”）
  POST /build    {packId,game,picks} 一次性编译成 .cvpack.json（picks 可引用 scan 的编号）
  POST /scene    {packId,scene[,present,absent]}  加场景样本；给了 present/absent 就顺手学阈值
  POST /recall   {packId,hwnd|path}  运行时：一帧 -> 盘面结构（默认 10 个实体 <50ms）
  POST /list                         列出所有包
  POST /get      {packId}            看一个包的元信息（不含模板像素）
  POST /delete   {packId}            删包
  POST /frame    {hwnd|path}         只抓一帧落盘 PNG（给模型偶尔真看图）
  POST /text     {box:[x,y,w,h],alphabet?}  读一块区域上的文字（内置字形模型，无需先学）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import wincap  # noqa: E402
from pcv import compile as C  # noqa: E402
from pcv import observe as OB  # noqa: E402
from pcv import pack as P  # noqa: E402
from pcv import propose as PR  # noqa: E402
from pcv import frame as FR  # noqa: E402
from pcv import input as INP  # noqa: E402

ROOT = os.path.join(os.path.expanduser("~"), ".dsh", "cv-recall")
FRAME_DIR = os.path.join(tempfile.gettempdir(), "cv-recall-frames")
LAST_SCAN: dict = {}


# ------------------------------------------------------------------ helpers

def to_rgb(arr) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim == 2:
        a = np.stack([a] * 3, axis=-1)
    if a.shape[2] == 4:
        a = a[:, :, :3]
    return np.ascontiguousarray(a.astype(np.uint8))


def grab(body: dict):
    """按 path / hwnd / title 取一帧 → (rgb, 描述)。"""
    path = (body.get("path") or "").strip()
    if path:
        with Image.open(path) as im:
            return to_rgb(np.asarray(im.convert("RGB"))), {"source": "file", "path": path}
    hwnd = wincap.resolve_window(int(body.get("hwnd") or 0), str(body.get("title") or ""))
    rgb = wincap.capture_client(hwnd)
    if rgb.size == 0:
        raise RuntimeError("抓到的画面是空的")
    return rgb, {"source": "window", "hwnd": hwnd, "title": wincap.window_title(hwnd)}


def save_frame(rgb: np.ndarray, tag: str = "f") -> str:
    os.makedirs(FRAME_DIR, exist_ok=True)
    p = os.path.join(FRAME_DIR, f"{tag}-{int(time.time() * 1000)}.png")
    Image.fromarray(rgb).save(p)
    return p


def pack_meta(pk: dict) -> dict:
    scenes = [s.get("name") for s in (pk.get("scenes") or [])]
    return {
        "packId": pk.get("id"),
        "game": pk.get("game"),
        "frame": pk.get("frame_size"),
        "scenes": scenes,
        "entities": [
            {"id": e.get("id"), "label": e.get("label"), "kind": e.get("kind"),
             "box": e.get("box"), "ar": round(float(e.get("ar") or 0), 3),
             "risk": e.get("risk")}
            for e in (pk.get("entities") or [])
        ],
        "created": pk.get("created"),
        "note": pk.get("note"),
    }


class NoPack(ValueError):
    """没有可用的感知包。`/recall` 会把它降级成「空画面帧」，别的路由当参数错误报。"""


def require_pack(body: dict, soft: bool = False):
    """要一个包：给了 id 就用它；没给且只有一个包就自动用它。

    `soft=True` 时"一个包都没有"返回 None（调用方自己降级），别的毛病照旧报错——
    缺引导的是"还没编译过"，不是"你自己写错了"。
    """
    pid = str(body.get("packId") or body.get("id") or "").strip()
    packs = [p.get("id") for p in P.list_packs(ROOT)]
    if pid:
        return P.load_pack(ROOT, pid)
    if len(packs) == 1:
        return P.load_pack(ROOT, packs[0])
    if not packs:
        if soft:
            return None
        raise NoPack('一个感知包都还没有：先 cv_learn{action:"scan"} 看候选框，'
                     '再 cv_learn{action:"build"} 把你要认的东西编译进去')
    raise NoPack("有好几个包，得说清用哪个：packId 取 " + "、".join(str(x) for x in packs)
                 + '（cv_learn{action:"list"} 能看到每个包是哪个游戏/界面）')


def require_capture_rgb(body: dict) -> np.ndarray:
    rgb = body.get("_rgb")
    if rgb is not None:
        return rgb
    raise ValueError("内部错误：缺少 rgb")


# ------------------------------------------------------------------- routes

def do_scan(body: dict) -> dict:
    rgb, src = grab(body)
    t0 = time.perf_counter()
    cands = PR.propose(rgb, limit=int(body.get("limit") or 12))
    rows = PR.describe(rgb, cands)
    ms = (time.perf_counter() - t0) * 1000.0
    p = save_frame(rgb, tag="scan")
    LAST_SCAN.clear()
    LAST_SCAN.update({"rgb": rgb, "cands": cands, "path": p, "at": time.time()})
    return {
        "ok": True,
        "frame": {"w": int(rgb.shape[1]), "h": int(rgb.shape[0]), "path": p},
        "source": src,
        "candidates": rows,
        "ms": round(ms, 1),
        "note": "候选框是本地算法免费给的；用编号挑你要的实体，不必再看图。",
    }


def _resolve_picks(body: dict, rgb: np.ndarray):
    raw = body.get("picks") or []
    if not raw:
        raise ValueError("需要 picks=[{n:2,id:'hp',label:'血条'}] 或 [{id,box:[x,y,w,h]}]")
    cands = LAST_SCAN.get("cands") if (time.time() - float(LAST_SCAN.get("at") or 0) < 900) else None
    out = []
    for i, p in enumerate(raw):
        item = dict(p)
        if "box" not in item:
            n = int(item.get("n") or 0)
            if not cands:
                raise ValueError(f"第 {i + 1} 个 pick 用了编号 n={n}，但最近没有 scan 结果；请先 cv_learn action=scan")
            if n < 1 or n > len(cands):
                raise ValueError(f"编号 n={n} 超出范围（scan 给了 {len(cands)} 个候选，1..{len(cands)}）")
            item["box"] = cands[n - 1]["box"]
        item.setdefault("id", item.get("label") or f"e{i + 1}")
        out.append(item)
    return out


def do_build(body: dict) -> dict:
    rgb, src = grab(body)
    picks = _resolve_picks(body, rgb)
    pack_id = str(body.get("packId") or body.get("id") or f"pack{int(time.time())}").strip()
    game = str(body.get("game") or pack_id).strip()
    scene = str(body.get("scene") or "default").strip()
    t0 = time.perf_counter()
    pk = C.build(ROOT, pack_id, game, rgb, picks, scene_name=scene,
                 note=str(body.get("note") or ""),
                 append=bool(body.get("append")),
                 present=body.get("present"),
                 derived=body.get("derived"))
    ms = (time.perf_counter() - t0) * 1000.0
    out = pack_meta(pk)
    out.update({"ok": True, "source": src, "ms": round(ms, 1), "append": bool(body.get("append")),
                "scene": scene,
                "framePath": save_frame(rgb, tag="build"),
                "diag": {e["id"]: e.get("diag") for e in pk.get("entities") or []}})
    return out


def do_scene(body: dict) -> dict:
    pk = require_pack(body)
    rgb, src = grab(body)
    name = str(body.get("scene") or "").strip()
    if not name:
        raise ValueError("需要 scene=场景名")
    def _ids(key):
        v = body.get(key)
        if v is None:
            return None
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"{key} 要给实体 id 数组，例如 [\"portrait\",\"hp_frame\"]")
        return {str(x) for x in v}
    want, nope = _ids("present"), _ids("absent")
    have = {e.get("id") for e in pk.get("entities") or []}
    if want is not None and want - have:
        raise ValueError(f"present 里有不存在的实体：{sorted(want - have)}")
    if nope is not None and nope - have:
        raise ValueError(f"absent 里有不存在的实体：{sorted(nope - have)}")
    sample = C.add_scene(pk, rgb, name, force=bool(body.get("force")),
                         present=None if want is None else sorted(want),
                         absent=None if nope is None else sorted(nope))
    P.save_pack(ROOT, pk)
    ents = pk.get("entities") or []
    # presence 是**按实体顺序**的 0/1 数组（包里存的就是这个紧凑形式），
    # 但工具输出要给人/给模型看，所以顺手翻成 {id: bool}。
    return {"ok": True, "packId": pk["id"], "scene": name, "source": src,
            "presence": {e.get("id"): bool(sample["presence"][i])
                         for i, e in enumerate(ents) if i < len(sample["presence"])},
            "conf": {e.get("id"): round(float(sample["conf"][i]), 3)
                     for i, e in enumerate(ents) if i < len(sample["conf"])},
            "tau": {e["id"]: e.get("tau") for e in ents},
            "scenes": [s.get("name") for s in pk.get("scenes") or []]}


def do_recall(body: dict) -> dict:
    t0 = time.perf_counter()
    pk = require_pack(body, soft=True)
    rgb, src = grab(body)
    want_boxes = bool(body.get("boxes"))
    if pk is None:
        # 没有包不是"错误"，是"这个窗口还没编译过"。照样给它一帧：帧号可以拿去点坐标，
        # 顺手读的文字块可以按文字点——总比一句 Error 有用。
        out = {"scene": "unlabeled", "scene_conf": 0.0, "scene_dist": 0.0,
               "entities": [], "conf": [], "changed": -1, "stale": False,
               "unlabeled": True,
               "hint": '这个窗口还没有感知包，认不出实体。先 cv_learn{action:"scan"} 再 build 一个包；'
                       '现在只能用坐标 click:{x,y}，或下面读到的文字块（t1/t2…）指路。'}
    else:
        out = OB.observe(pk, rgb, {"detail": bool(body.get("detail")),
                                   "boxes": True,
                                   "values": bool(body.get("values")),
                                   "labels": bool(body.get("labels")),
                                   "novel": bool(body.get("novel"))})
        out["packId"] = pk["id"]
        out["entities"] = [
            {"id": e.get("id"),
             "present": out["entities"][i],
             "conf": out["conf"][i],
             "label": e.get("label")}
            for i, e in enumerate(pk.get("entities") or [])
        ]
    out["ok"] = True
    tspec = body.get("text")
    if isinstance(tspec, str) and tspec.lower() in ("bottom", "top", "mid", "full"):
        from pcv import text as Txt
        out["text"] = [_read_region(rgb, Txt.band_box(rgb.shape[0], rgb.shape[1], tspec))]
        out["text_band"] = tspec.lower()
    elif isinstance(tspec, bool) and tspec:
        from pcv import text as Txt
        out["text"] = [_read_region(rgb, Txt.band_box(rgb.shape[0], rgb.shape[1], "bottom"))]
        out["text_band"] = "bottom"
    else:
        tf = _text_fields(tspec)
        if tf:
            out["text"] = [_read_region(rgb, t["box"], t.get("alphabet"), t.get("label")) for t in tf]
    if src.get("hwnd"):
        out["hwnd"] = src["hwnd"]

    # 登记这一帧：之后所有动作都只用「帧里的 id / 文字」指路，模型不必记坐标。
    texts = []
    for region in (out.get("text") or []):
        blocks = region.get("blocks")
        if not blocks and region.get("box") and region.get("text"):
            blocks = [{"text": region.get("text"), "box": region.get("box"),
                       "kind": region.get("kind"), "value": region.get("value")}]
            region["blocks"] = blocks
        for b in (blocks or []):
            tid = "t%d" % (len(texts) + 1)
            b["id"] = tid
            texts.append({"id": tid, "text": b.get("text"), "box": b.get("box"),
                          "kind": b.get("kind"), "value": b.get("value")})
    rec = FR.new(hwnd=src.get("hwnd") or wincap.foreground(), rgb=rgb,
                 pack_id=(pk["id"] if pk else None),
                 boxes=out.get("boxes"), texts=texts, scene=out.get("scene"),
                 labels={} if pk is None else
                 {e.get("id"): (e.get("label") or e.get("id")) for e in pk.get("entities") or []})
    out["frame"] = rec["id"]
    if pk is None:
        out["ms"] = int(round((time.perf_counter() - t0) * 1000.0))
    if not want_boxes:
        out.pop("boxes", None)
    return out


# ---------------------------------------------------------------- 手：按 id 干活
INPUT_ENABLED = [True]        # 默认允许动手（用户拍板）；--no-input / 配置可关
_STOP = [False]


def do_stop(body: dict = None) -> dict:
    """中止批处理并把按住的键全抬起来（Ctrl 粘住是能坑死人的）。"""
    _STOP[0] = True
    n = INP.release_all()
    return {"ok": True, "released": n,
            "did": ("已停手，放开 %d 个按住的键" % n) if n else "已停手"}


def _pick_target(body: dict):
    for k in ("click", "target", "at", "point", "id"):
        if body.get(k) is not None:
            return body[k]
    return None


def _point_of(rec, target, space="screen"):
    """把目标解析成**客户区像素**坐标。

    `space` 只对"裸坐标"（{x,y} / "925,512"）有意义：默认按屏幕绝对坐标理解（和 coach_* 一致），
    内部减掉客户区原点换成画面坐标；`space="client"` 时才当成已经算好的画面坐标。
    为什么要这么做：同一帧里 (925,512) 在 coach 那边是骰子、在 cv 这边是空地——两套坐标混用
    是本轮点错位置的直接原因。
    """
    box, how, note = FR.resolve_target(rec, target)
    if how in ("point", "screen"):
        x, y = int(box[0]), int(box[1])
        if how == "point" and space == "screen":
            ox, oy = wincap.client_rect(rec["hwnd"])[:2]
            x, y = x - ox, y - oy
        return x, y, how, note
    cx = box[0] + max(1, box[2]) // 2
    cy = box[1] + max(1, box[3]) // 2
    return int(cx), int(cy), how, note


def _frame_for(body: dict):
    """要一帧：优先用指定的帧号，其次用同窗口最近一帧，最后**现抓一帧**。

    最后这条兜底是为了让"只按坐标点一下"不必先 recall（但正常用法应该带帧号，
    那样模型才能用 id/文字指路，也不怕画面已经变了）。
    """
    fid = body.get("frame")
    if fid:
        return FR.get(fid), False
    rec = FR.latest(body.get("hwnd"))
    if rec is not None:
        return rec, True
    hwnd = int(body.get("hwnd") or 0)
    title = str(body.get("title") or "")
    if not hwnd and not title:
        hwnd = wincap.foreground()
    if not hwnd:
        raise KeyError("还没有帧，也没有指定窗口：先 cv_recall 一次，或者给 hwnd/title")
    hwnd = wincap.resolve_window(hwnd, title)
    rgb = wincap.capture_client(hwnd)
    return FR.new(hwnd, rgb, pack_id=None, boxes={}, texts=[], scene=None), True


def _verify_after(rec, hwnd, verify: str, settle: int):
    """动作后看一眼：变了几格；verify=scene 时再跑一遍感知包拿场景与实体。"""
    if verify == "none":
        return {}, None
    time.sleep(max(0, int(settle)) / 1000.0)
    rgb = wincap.capture_client(hwnd)
    d = FR.diff(rec, rgb)
    info = {"changed": d["changed"], "boxes": d.get("boxes") or []}
    if verify in ("scene", "full") and rec.get("pack"):
        try:
            pk = P.load_pack(ROOT, rec["pack"])
            o = OB.observe(pk, rgb, {"boxes": True})
            info["scene"] = o.get("scene")
            if rec.get("scene") and rec.get("scene") != o.get("scene"):
                info["scene_from"] = rec.get("scene")
            if verify == "full":
                info["stale"] = bool(o.get("stale"))
                info["ms_see"] = o.get("ms")
            boxes = o.get("boxes") or {}
            labels = {e.get("id"): (e.get("label") or e.get("id")) for e in pk.get("entities") or []}
            FR.new(hwnd=hwnd, rgb=rgb, pack_id=rec["pack"], boxes=boxes,
                   texts=[], scene=o.get("scene"), labels=labels)
            return info, boxes
        except Exception as e:  # noqa: BLE001
            info["see_error"] = f"{type(e).__name__}: {e}"
    return info, None


def _do_one(body: dict) -> dict:
    if not INPUT_ENABLED[0]:
        return {"ok": False, "code": "INPUT_DISABLED",
                "error": "动手能力被关掉了（--no-input / 配置 inputEnabled=false）"}
    t0 = time.time()
    action = str(body.get("action") or "click").strip().lower()
    if action in ("stop", "abort"):
        return do_stop()
    rec, implied = _frame_for(body)
    hwnd = rec["hwnd"]
    allow = body.get("allow_windows")
    recover = bool(body.get("recover"))
    settle = int(body.get("settle_ms") if body.get("settle_ms") is not None else 200)
    verify = str(body.get("verify") or "changed").lower()
    dry = bool(body.get("dry_run"))
    space = str(body.get("coord_space") or body.get("coordSpace") or "screen").strip().lower()
    if space not in ("screen", "client"):
        raise ValueError("coordSpace 只能是 \"screen\"（默认，屏幕绝对坐标）或 \"client\"（画面坐标）")
    rec_used = False
    clicked = None

    # ---- 抢前台（排障报告 P0-1）
    # 为什么必须抢：前台属于别的进程时系统会**静默忽略** SetForegroundWindow，
    # 于是"回执写着前台是游戏、点击却落到 DSH"，而 guard_point 只看"顶层窗口"，
    # 分不清"被 DSH 盖住"和"被游戏自己的子窗口盖住"。先把目标提到前面，
    # 再看那个点上最上面的到底是谁——只有确认是目标自己，才允许点下去。
    force_fg = body.get("force_foreground")
    force_fg = True if force_fg is None else bool(force_fg)
    fg_pre = None
    if not dry and force_fg:
        got = False
        try:
            got = wincap.set_foreground(hwnd)
        except Exception:  # noqa: BLE001
            got = False
        fg_pre = wincap.foreground()
        if not got and not recover:
            _t = wincap.window_title(fg_pre) if fg_pre else ""
            return {"ok": False, "code": "NOT_FOREGROUND",
                    "error": "抢不到前台：请求 %d，真实前台是 %d（%s）。把游戏点一下让它到前台，"
                             "或给 recover:true 再试。" % (hwnd, fg_pre, _t or "无标题"),
                    "frame": rec["id"], "requested_hwnd": hwnd,
                    "actual_foreground_hwnd": fg_pre, "actual_foreground_title": _t,
                    "ms": int((time.time() - t0) * 1000)}

    # ---- 算点（先算后验：拒绝时也要能说清"本来想点哪儿"）
    point = None
    target = _pick_target(body)
    if action in ("click", "double_click", "right_click", "move", "scroll", "drag"):
        if action == "drag":
            a = body.get("from")
            b = body.get("to")
            if a is None or b is None:
                raise ValueError("drag 要给 from 和 to（各自可以是 id/文字/{x,y}）")
            p1 = _point_of(rec, a, space)
            p2 = _point_of(rec, b, space)
            point = (p1, p2)
        else:
            if target is None:
                raise ValueError("没说要动哪儿：给 click:\"<id或文字>\" 或 click:{x,y}")
            point = _point_of(rec, target, space)

    # ---- 守卫（屏幕坐标现算）
    def _check(px, py):
        if dry:
            return True, "", "", False
        return FR.guard_point(rec, px, py, allow_windows=allow, recover=recover)

    if action == "drag":
        (x1, y1, how1, note1), (x2, y2, how2, note2) = point
        ok, code, msg, rec_used = _check(x1, y1)
        if not ok:
            return {"ok": False, "code": code, "error": msg, "frame": rec["id"],
                    "would_click": [x1, y1], "ms": int((time.time() - t0) * 1000)}
        sx1, sy1 = wincap.client_to_screen(hwnd, x1, y1)
        sx2, sy2 = wincap.client_to_screen(hwnd, x2, y2)
        if not dry:
            INP.drag(sx1, sy1, sx2, sy2, button=str(body.get("button") or "left"))
        did = "拖动 (%d,%d)→(%d,%d)" % (sx1, sy1, sx2, sy2)
        how, note = "%s→%s" % (how1, how2), "%s→%s" % (note1, note2)
    elif action in ("key", "type", "text"):
        if action == "key":
            chord = body.get("key") or "space"
            rep = int(body.get("repeat") or 1)
            hold = int(body.get("hold_ms") or 0)
            if not dry:
                INP.key(str(chord), repeat=rep, hold_ms=hold)
            did = "按键 %s%s%s" % (chord, (" ×%d" % rep) if rep > 1 else "",
                                   (" 按住 %dms" % hold) if hold else "")
            how, note = "key", str(chord)
        else:
            txt = body.get("text")
            if not txt:
                raise ValueError("type 要给 text（要打的字）")
            if not dry:
                INP.type_text(str(txt), delay_ms=int(body.get("delay_ms") or 0))
            did = "输入「%s」" % (str(txt)[:20] + ("…" if len(str(txt)) > 20 else ""))
            how, note = "type", ""
        if not dry and wincap.foreground() != hwnd:
            return {"ok": False, "code": "NOT_FOREGROUND",
                    "error": "目标窗口不在前台，键盘会被发到别处。用 recover:true 切过去",
                    "frame": rec["id"], "ms": int((time.time() - t0) * 1000)}
    else:
        x, y, how, note = point
        ok, code, msg, rec_used = _check(x, y)
        if not ok:
            return {"ok": False, "code": code, "error": msg, "frame": rec["id"],
                    "would_click": [x, y], "ms": int((time.time() - t0) * 1000)}
        sx, sy = wincap.client_to_screen(hwnd, x, y)
        clicked = [sx, sy]
        if action == "move":
            if not dry:
                INP.move(sx, sy)
            did = "鼠标移到 (%d,%d)" % (sx, sy)
        elif action == "scroll":
            if not dry:
                INP.move(sx, sy)
                INP.scroll(str(body.get("direction") or "down"), int(body.get("clicks") or 3))
            did = "在 (%d,%d) 滚%s %d 格" % (sx, sy, body.get("direction") or "down",
                                            int(body.get("clicks") or 3))
        else:
            btn = "right" if action == "right_click" else str(body.get("button") or "left")
            cnt = 2 if action == "double_click" else int(body.get("count") or 1)
            hold = int(body.get("hold_ms") or 0)
            if not dry:
                INP.click(sx, sy, button=btn, count=cnt, hold_ms=hold)
            did = "点%s (%d,%d)" % (("右键" if btn == "right" else ("双击" if cnt == 2 else "")), sx, sy)

    info, boxes = _verify_after(rec, hwnd, verify, settle)
    out = {"ok": True, "did": did, "frame": rec["id"], "how": how}
    if note:
        out["target"] = str(note)[:24]
    if rec_used:
        out["recovered"] = True
    out.update({k: v for k, v in info.items() if k not in ("changed", "boxes")})
    if "changed" in info:
        out["changed"] = info["changed"]
        out["changedTiles"] = info["changed"]
    if info.get("boxes"):
        out["changedRegion"] = info["boxes"]
    # 排障报告 P0-1：回执里必须能读到"我请求的是谁、真实前台是谁、点在屏幕哪个点"
    fg_post = wincap.foreground()
    out["coordSpace"] = space
    out["requested_hwnd"] = hwnd
    out["actual_foreground_hwnd"] = fg_post
    out["actual_foreground_title"] = wincap.window_title(fg_post) if fg_post else ""
    out["foreground_ok"] = (fg_post == hwnd)
    if clicked:
        out["clicked_screen_xy"] = clicked
    if not dry:
        out["moveSettleMs"] = int(body.get("move_settle_ms") or 0)
        out["downUpMs"] = int(body.get("down_up_ms") or 0)
    if not out["foreground_ok"]:
        out["verdict"] = "foreground"
        out["hint"] = ("动手后前台不在目标窗口上（现在是「%s」%s）：这一下很可能落到了别的窗口。"
                       % (out["actual_foreground_title"] or "无标题", fg_post))
    elif out.get("changedTiles") == 0:
        out["verdict"] = "no_change"
        out["hint"] = ("动了但画面没变：可能未命中（点偏）/ 被禁用 / 被遮挡。"
                       "先 cv_recall 看这一帧的 boxes，或用 detail 确认元素在不在。")
    out["ms"] = int((time.time() - t0) * 1000)

    # 动作后这一帧登记成新帧：变化不大就把旧框带过去（布局没动），变化大就不带（让模型重新看）
    if not dry:
        try:
            after = wincap.capture_client(hwnd)
            carry = {}
            if boxes:
                carry = boxes
                out["frame"] = FR.new(hwnd=hwnd, rgb=after, pack_id=rec.get("pack"), boxes=boxes,
                                      texts=[], scene=info.get("scene") or rec.get("scene"),
                                      labels=rec.get("labels"))["id"]
            elif int(info.get("changed") or 0) <= 6:
                nr = FR.new(hwnd=hwnd, rgb=after, pack_id=rec.get("pack"), boxes=rec["boxes"],
                            texts=rec["texts"], scene=rec.get("scene"), labels=rec.get("labels"))
                nr["carried"] = True
                out["frame"] = nr["id"]
                out["carried"] = True
        except Exception as e:  # noqa: BLE001
            out["frame_note"] = f"动作已执行，但新帧没登记上：{type(e).__name__}: {e}"
    return out


def _do_batch(body: dict) -> dict:
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("batch 要给 steps（动作数组）")
    _STOP[0] = False
    pause = int(body.get("pause_ms") if body.get("pause_ms") is not None else 60)
    deadline = float(body.get("deadline_ms") or 8000)
    t0 = time.time() * 1000.0
    done = []
    for i, st in enumerate(steps):
        if _STOP[0]:
            return {"ok": False, "code": "STOPPED", "done": done,
                    "error": "批处理在收到停止后中断", "at": i}
        if time.time() * 1000 - t0 > deadline:
            return {"ok": False, "code": "DEADLINE", "done": done,
                    "error": f"批处理超过 {int(deadline)}ms 还没走完，在第 {i + 1} 步停下"}
        st = dict(st or {})
        st.setdefault("frame", body.get("frame"))
        st.setdefault("verify", body.get("verify") or "changed")
        try:
            r = _do_one(st)
        except KeyError as e:
            r = {"ok": False, "code": "NOT_FOUND", "error": str(e).strip("'\"")}
        except ValueError as e:
            r = {"ok": False, "code": "BAD_ARGS", "error": str(e)}
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "code": "FAILED", "error": f"{type(e).__name__}: {e}"}
        line = r.get("did") or r.get("error") or "?"
        done.append({"step": i + 1, "ok": bool(r.get("ok")), "did": line})
        if not r.get("ok"):
            return {"ok": False, "code": r.get("code") or "STEP_FAILED", "done": done,
                    "error": f"第 {i + 1} 步失败：{r.get('error')}", "at": i}
        if r.get("frame"):
            body["frame"] = r["frame"]
        if pause:
            time.sleep(max(0, pause) / 1000.0)
    return {"ok": True, "done": done, "frame": body.get("frame"),
            "ms": int((time.time() - t0) * 1000)}


def _log_act(body: dict, out: dict) -> None:
    """每次动手都留一行审计日志：它动了哪个窗口、点了什么、成不成。

    这是给自己（和人）事后复核用的：模型点错了，能查出它当时手里是哪一帧、哪个 id。
    """
    try:
        short = {k: v for k, v in body.items() if k in
                 ("action", "frame", "click", "key", "text", "direction", "clicks", "from", "to", "verify")}
        if isinstance(short.get("text"), str):
            short["text"] = short["text"][:200]
        if body.get("steps"):
            short["steps"] = len(body["steps"])
        rec = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "in": short,
            "ok": bool(out.get("ok")),
            "code": out.get("code"),
            "did": out.get("did"),
            "target": out.get("target"),
            "frame": out.get("frame"),
            "changed": out.get("changed"),
            "steps_done": len(out.get("done") or []),
            "stopped_at": out.get("at"),
        }
        with open(os.path.join(ROOT, "input-log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  日志失败绝不能影响动作
        pass


def do_act(body: dict) -> dict:
    """动作入口：**永远返回结构化结果**，不抛异常。

    失败对模型来说必须是"能读的一句话 + 一个码"，而不是 500；它据此决定是重看一帧、
    换个目标，还是停下来问人——抛栈做不到这件事。
    """
    action = str(body.get("action") or "click").strip().lower()
    # /stop 只中止进行中的批处理并放开按键，不是永久停用：新的一次动作自动复位。
    # 真要彻底停用，用 --no-input 启动（那是配置，不是运行期开关）。
    if action not in ("batch", "steps", "stop"):
        _STOP[0] = False
    try:
        out = _do_batch(body) if action in ("batch", "steps") else _do_one(body)
    except KeyError as e:
        return {"ok": False, "code": "NOT_FOUND", "error": str(e).strip("'\"")}
    except ValueError as e:
        return {"ok": False, "code": "BAD_ARGS", "error": str(e)}
    except RuntimeError as e:
        msg = str(e)
        code = "REFUSED" if ("SendInput" in msg or "前台" in msg) else "FAILED"
        return {"ok": False, "code": code, "error": msg}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "FAILED", "error": f"{type(e).__name__}: {e}"}
    out.setdefault("ok", False)
    _log_act(body, out)
    return out


def _text_fields(spec):
    """`text` 参数：`[x,y,w,h]` / `"x,y,w,h"` / `[{box:[...], label:"名字"}]`，最多 6 块。"""
    if not spec:
        return []
    if isinstance(spec, dict):
        spec = [spec]
    if spec and isinstance(spec[0], (int, float)):
        spec = [spec]
    out = []
    for it in list(spec)[:6]:
        if isinstance(it, str):
            it = {"box": [int(v) for v in it.replace(",", " ").split()]}
        box = it.get("box") if isinstance(it, dict) else it
        if not box or len(box) != 4:
            raise ValueError("text 参数要写成 [x,y,w,h] 或 {box:[x,y,w,h],label:\"名字\"}")
        out.append({"box": [int(v) for v in box],
                    "label": (it.get("label") if isinstance(it, dict) else None),
                    "alphabet": (it.get("alphabet") if isinstance(it, dict) else None)})
    return out


def _read_region(rgb, box, alphabet=None, label=None):
    """读一块区域的文字。

    分工：**普通文字走系统 OCR**（`Windows.Media.Ocr`，中文也能认，热路径 ~20ms），
    **数字/字母指定 alphabet 时走内置字形模型**（花体、像素字、超大描边这些系统 OCR
    认不出的，正是自己训的模型擅长的）。系统 OCR 认不出时自动退到字形模型，两路都
    认不出就返回空串——**不猜**。
    """
    from pcv import ocrlib
    from pcv import text as Txt
    H, W = rgb.shape[:2]
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1 = min(W, x0 + max(1, int(box[2])))
    y1 = min(H, y0 + max(1, int(box[3])))
    if x1 - x0 < 6 or y1 - y0 < 6:
        raise ValueError(f"文字框 {list(box)} 太小或落在画面外")
    crop = rgb[y0:y1, x0:x1]
    o = {"box": [x0, y0, x1 - x0, y1 - y0]}
    if label:
        o["label"] = label
    alpha = str(alphabet or "").lower()
    if alpha in ("digits", "num", "0-9"):
        txt, _det = ocrlib.read_text(crop, alphabet=ocrlib.DIGITS)
        o.update({"text": txt, "via": "glyph"})
        return o
    try:
        rb = Txt.read_blocks(rgb, box=o["box"], verify=True)
    except Exception as exc:            # noqa: BLE001  没语言包/桥挂了都要能退
        rb = {"text": "", "blocks": [], "lines": [], "ms": 0, "err": str(exc)}
    if rb.get("text"):
        o.update({"text": rb["text"], "blocks": rb.get("blocks") or [],
                  "lines": rb.get("lines") or [], "via": "ocr", "ms": rb.get("ms")})
        if rb.get("dropped"):
            o["dropped"] = rb["dropped"]        # 两遍缩放读不一致、被筛掉的块数
        return o
    txt, _det = ocrlib.read_text(crop, alphabet=None) if (x1 - x0) <= 320 and (y1 - y0) <= 160 \
        else ("", [])
    o.update({"text": txt, "via": "glyph" if txt else "-"})
    if rb.get("err"):
        o["note"] = "系统 OCR 不可用：" + str(rb["err"])
    elif not txt:
        o["note"] = "这一片看不出像文字（系统 OCR 只读出零散噪点，已丢弃）"
    return o


def _read_one(rgb, t):
    """读一个 `_text_fields` 出来的条目（带可选 alphabet / label）。"""
    return _read_region(rgb, t["box"], t.get("alphabet"), t.get("label"))


def do_text(body: dict) -> dict:
    """单独读文字。

    `{box:[x,y,w,h]}` 读指定区域（可加 `alphabet:"digits"`）；不给 box 时读一条**横向
    长条**：`band:"bottom"|"top"|"mid"|"full"`，缺省 bottom（对话框、选项、操作提示都
    在下面，只读下半屏比全屏快一个数量级）。
    """
    from pcv import text as Txt
    rgb, src = grab(body)
    out = {"ok": True, "source": src}
    spec = body.get("box") or body.get("text")
    band = body.get("band")
    if isinstance(spec, str) and spec.lower() in Txt.BANDS:
        band, spec = spec, None
    if spec:
        t = _text_fields(spec)[0]
        out.update(_read_region(rgb, t["box"],
                                body.get("alphabet") or t.get("alphabet"), t.get("label")))
        return out
    band = str(band or ("full" if body.get("full") else "bottom")).lower()
    box = Txt.band_box(rgb.shape[0], rgb.shape[1], band)
    out["band"] = band
    out.update(_read_region(rgb, box, body.get("alphabet")))
    return out


def do_list(body: dict) -> dict:
    # list_packs 返回的是**摘要**（entities 已经是数量），不要再去 len() 一遍。
    packs = [{"packId": p.get("id"), "game": p.get("game"),
              "entities": int(p.get("entities") or 0),
              "scenes": p.get("scenes") or []}
             for p in P.list_packs(ROOT)]
    return {"ok": True, "root": ROOT, "packs": packs}


def do_get(body: dict) -> dict:
    pk = require_pack(body)
    out = pack_meta(pk)
    out.update({"ok": True, "tau": {e["id"]: e.get("tau") for e in pk.get("entities") or []},
                "w": {e["id"]: e.get("w") for e in pk.get("entities") or []}})
    return out


def do_delete(body: dict) -> dict:
    pk = require_pack(body)
    P.delete_pack(ROOT, pk["id"])
    return {"ok": True, "deleted": pk["id"]}


def do_frame(body: dict) -> dict:
    rgb, src = grab(body)
    return {"ok": True, "path": save_frame(rgb, tag="frame"), "source": src,
            "w": int(rgb.shape[1]), "h": int(rgb.shape[0])}


ROUTES = {
    "/scan": do_scan,
    "/build": do_build,
    "/scene": do_scene,
    "/recall": do_recall,
    "/act": do_act,
    "/stop": do_stop,
    "/list": do_list,
    "/get": do_get,
    "/delete": do_delete,
    "/frame": do_frame,
    "/text": do_text,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cvcd/0.1"

    def log_message(self, fmt, *args):  # 静音：日志走 stderr 会污染握手
        return

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, obj: dict):
        obj.setdefault("ok", True)
        self._send(200, obj)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path == "/health":
                return self._ok({"version": "0.1", "root": ROOT,
                                 "packs": len(P.list_packs(ROOT)),
                                 "screen": list(wincap.screen_size())})
            if path == "/windows":
                return self._ok({"windows": wincap.list_windows()})
            return self._send(404, {"ok": False, "error": "未知路径 " + path})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            return self._send(500, {"ok": False, "error": str(exc)})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_json()
        fn = ROUTES.get(path)
        if not fn:
            return self._send(404, {"ok": False, "error": "未知路径 " + path})
        try:
            return self._ok(fn(body))
        except ValueError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            return self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    ap = argparse.ArgumentParser(description="cvcd 感知编译器 sidecar")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--root", default="")
    ap.add_argument("--no-input", action="store_true",
                    help="关掉动手能力（只感知不动手）；默认允许")
    args = ap.parse_args()

    global ROOT
    INPUT_ENABLED[0] = not args.no_input
    if args.root:
        ROOT = os.path.abspath(args.root)
    os.makedirs(P.packs_dir(ROOT), exist_ok=True)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    port = srv.server_address[1]
    # 握手行必须单行、ASCII 安全（Windows 管道编码不可靠）
    sys.stdout.write(json.dumps({"ok": True, "port": port, "pid": os.getpid(),
                                 "root": ROOT, "screen": list(wincap.screen_size())}) + "\n")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    main()
