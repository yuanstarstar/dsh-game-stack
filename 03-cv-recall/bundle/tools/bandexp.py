#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验：把「整块模板」换成「四边带模板」，能不能救容器类实体。

背景：dialog_panel 在 choice 场景里内容整块被替换（一段话 -> 问题 + 3 个选项条），
整块灰度 NCC 从 0.91 掉到 0.38。而"面板还在不在"其实只需看边框。
这个脚本只量数字，不改生产代码。
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUG, "sidecar"))

from pcv import feats  # noqa: E402
from pcv import pack as P  # noqa: E402

UI = os.path.join(HERE, "ui")
ROOT = os.path.join(PLUG, ".tmp-selftest")
FRAMES = ["battle_a", "battle_b", "dialog_a", "dialog_b", "choice_a", "choice_b"]


def band_templates(tpl, frac=0.22):
    h, w = tpl.shape
    bh = max(3, int(round(h * frac)))
    bw = max(3, int(round(w * frac)))
    return [tpl[:bh, :], tpl[h - bh:, :], tpl[:, :bw], tpl[:, w - bw:]]


def band_score(work, tg, ws, box, search):
    """在真值位置上量四边带的平均分（和工作尺度域一致）。"""
    x = (box[0] - search[0]) / ws
    y = (box[1] - search[1]) / ws
    tw, th = tg.shape[1], tg.shape[0]
    x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
    # 直接在被测位置裁一个「模板大小 + 4」的窗，用局部窗做带匹配
    pad = 2
    wx0, wy0 = max(0, x0 - pad), max(0, y0 - pad)
    win = work[wy0:y0 + th + pad, wx0:x0 + tw + pad]
    if win.shape[0] < th or win.shape[1] < tw:
        return None
    maps = []
    for b in band_templates(tg):
        m = feats.ncc_map(win, b)
        if m is not None and m.size:
            maps.append(m)
    if not maps:
        return None
    hh = min(m.shape[0] for m in maps)
    ww = min(m.shape[1] for m in maps)
    m = np.mean([x_[:hh, :ww] for x_ in maps], axis=0)
    i = int(np.argmax(m))
    return float(m.ravel()[i]), (i % m.shape[1], i // m.shape[1])


def main():
    pk = P.load_pack(ROOT, "fakegame")
    imgs = {n: np.asarray(Image.open(os.path.join(UI, n + ".png")).convert("RGB"))
            for n in FRAMES}
    for eid in ("dialog_panel", "minimap", "name_plate", "portrait"):
        e = [x for x in pk["entities"] if x["id"] == eid][0]
        tg = feats.to_gray(P.decode_png(e["template"]["png"]))
        ws = float(e["ws"])
        sx, sy, sw, sh = e["search"]
        print(f"\n{eid}  ws={ws}  box={e['box']}  tpl={tg.shape[::-1]}")
        print(f"  {'帧':<12}{'四带分':>8}  位置偏移")
        for n in FRAMES:
            fr = imgs[n]
            crop = feats.to_gray(fr[sy:sy + sh, sx:sx + sw])
            if ws > 1.02:
                gw = max(12, int(round(sw / ws)))
                gh = max(12, int(round(sh / ws)))
                work = feats.resize_area(crop, gw, gh)
            else:
                work = crop
            r = band_score(work, tg, ws, e["box"], e["search"])
            if r is None:
                print(f"  {n:<12}{'—':>8}")
            else:
                s, (dx, dy) = r
                print(f"  {n:<12}{s:>8.3f}  ({dx},{dy})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
