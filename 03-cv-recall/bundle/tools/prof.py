#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profile 一次 cv_recall：谁在吃时间。用法：python tools/prof.py [图名]"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUG, "sidecar"))

from pcv import observe as O  # noqa: E402
from pcv import pack as P  # noqa: E402

UI = os.path.join(HERE, "ui")
ROOT = os.path.join(PLUG, ".tmp-selftest")


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "battle_b.png"
    pk = P.load_pack(ROOT, "fakegame")
    frame = np.asarray(Image.open(os.path.join(UI, name)).convert("RGB"))
    O.observe(pk, frame)          # 预热（changed 状态、模板解码缓存）

    t = []
    for _ in range(3):
        import time
        t0 = time.perf_counter()
        O.observe(pk, frame)
        t.append((time.perf_counter() - t0) * 1000)
    print(f"{name}  单次 {min(t):.1f}ms (3 次 min)")

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(3):
        O.observe(pk, frame)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(14)
    print(s.getvalue())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
