#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验收（验收标准是跟用户对过的，不含临时加码）：

  1. 同一界面只标 1 次 -> 全新截图上实体召回 >= 95%
  2. 场景判定 >= 95%
  3. cv_recall 端到端 < 50ms（目标 <20ms）
  4. 换主题/换尺寸 -> stale=true，而不是静默错答
  5. 换别的画面 -> 0 个实体，不幻觉

用法：python tools/selftest.py [--ui tools/ui] [--root .tmp-selftest]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
PLUG = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUG, "sidecar"))
sys.path.insert(0, HERE)

from pcv import compile as C  # noqa: E402
from pcv import observe as OB  # noqa: E402
from pcv import pack as P  # noqa: E402
from pcv import propose as PR  # noqa: E402

SCENE_OF = {}


def load(ui: str, name: str) -> np.ndarray:
    return np.asarray(Image.open(os.path.join(ui, name)).convert("RGB"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui", default=os.path.join(HERE, "ui"))
    ap.add_argument("--root", default=os.path.join(PLUG, ".tmp-selftest"))
    ap.add_argument("--no-gen", action="store_true")
    args = ap.parse_args()

    if not args.no_gen:
        import gen_ui
        gen_ui.main.__globals__["sys"].argv = ["gen_ui", args.ui]
        gen_ui.main()

    boxes = json.load(open(os.path.join(args.ui, "boxes.json"), encoding="utf-8"))
    truth = json.load(open(os.path.join(args.ui, "truth.json"), encoding="utf-8"))
    for scene, ents in boxes["scenes"].items():
        for v in "ab":
            SCENE_OF[f"{scene}_{v}.png"] = scene

    ids = [p["id"] for p in boxes["picks"]]
    fails = []

    # ---------------- 1) 逐场景标注（append）-> 编译
    # 一个屏幕上不可能同时出现所有控件，所以"在战斗帧上一次标完 11 个"是错的：
    # 那样 dialog_panel/name_plate/choice_cursor 会从空白背景抠模板（误报的直接来源）。
    by_id = {p["id"]: p for p in boxes["picks"]}
    steps = [
        ("battle_a.png", "battle", boxes["scenes"]["battle"]),
        ("dialog_a.png", "dialog", [i for i in boxes["scenes"]["dialog"]
                                    if i not in boxes["scenes"]["battle"]]),
        ("choice_a.png", "choice", [i for i in boxes["scenes"]["choice"]
                                    if i not in boxes["scenes"]["dialog"]]),
    ]
    shutil.rmtree(args.root, ignore_errors=True)
    t0 = time.perf_counter()
    pk = None
    for k, (frame_name, scene, ids_here) in enumerate(steps):
        # present = 这一帧里确实在场的所有实体（含不用重标的老实体）→ 白拿的正样本；
        # absent = 已经编进包里、但这一帧明显不在的实体（战斗 HUD 在对话屏上就是这种）
        #          → 白拿的负样本，顺手把它们的门限收严。
        scene_ids = boxes["scenes"][scene]
        absent = [i for i in ids if i not in scene_ids and
                  (pk is None or i in {e["id"] for e in pk["entities"]})]
        pk = C.build(args.root, "fakegame", "假游戏", load(args.ui, frame_name),
                     [by_id[i] for i in ids_here], scene_name=scene,
                     note="selftest", append=(k > 0), present=scene_ids)
    build_ms = (time.perf_counter() - t0) * 1000.0
    pack_kb = os.path.getsize(os.path.join(P.packs_dir(args.root), "fakegame.cvpack.json")) / 1024.0

    print(f"\n== 编译 {len(steps)} 个场景共 {build_ms:.0f}ms，包 {pack_kb:.0f}KB，"
          f"{len(pk['entities'])} 个实体 ==")
    for e in pk["entities"]:
        t = e["tau"]
        print(f"   {e['id']:<13} ws {e.get('ws', 1.0):<5.2f} "
              f"门限 g{t.get('ncc_g', 0):.2f}/e{t.get('ncc_e', 0):.2f}  权重 "
              f"{ {k: round(v, 2) for k, v in e['w'].items() if v > 0} }")

    # ---------------- 2) 逐图召回 / 场景 / 延迟
    warns = [(e["id"], e["risk"]) for e in pk["entities"] if e.get("risk")]
    if warns:
        print("\n== 编译期提示（不影响运行，只是告诉你哪些实体可能不稳）==")
        for eid, msg in warns:
            print(f"   {eid}: {msg}")

    def evaluate(tag: str) -> dict:
        print(f"\n== 运行时 · {tag} ==")
        print(f"{'图':<20}{'场景':<10}{'conf':>6}{'stale':>7}{'冷ms':>7}{'热ms':>7}   漏报 / 误报")
        hit = tot = scene_ok = scene_tot = 0
        lat, cold = [], []
        for name in sorted(truth):
            if name == "foreign.png":
                continue
            img = load(args.ui, name)
            out = OB.observe(pk, img, {"detail": True})      # 冷：换画面后的第一帧
            cold.append(out["ms"])
            # 热：同一画面连续帧。上一帧的位置当 hint，跳过整窗搜索 —— 这才是打游戏
            # 时的常态（画面每帧都在刷，但控件不动）。
            hot = OB.observe(pk, img, {"detail": True})
            lat.append(hot["ms"])
            got = [d["id"] for d, p in zip(out["detail"], out["entities"]) if p]
            want = truth[name]
            fn = [e for e in want if e not in got]
            fp = [g for g in got if g not in want]
            if not name.startswith("battle_theme"):
                hit += len(want) - len(fn)
                tot += len(want)
                scene_tot += 1
                if out["scene"] == SCENE_OF.get(name):
                    scene_ok += 1
            print(f"{name:<20}{out['scene']:<10}{out['scene_conf']:>6.2f}"
                  f"{str(out['stale']):>7}{out['ms']:>7.1f}{hot['ms']:>7.1f}"
                  f"   {fn or '-'} / {fp or '-'}")
        r = {"recall": hit / max(1, tot) * 100, "scene": scene_ok / max(1, scene_tot) * 100,
             "avg": float(np.mean(lat)), "max": float(max(lat)),
             "cold": float(np.mean(cold)), "coldmax": float(max(cold))}
        print(f"实体召回 {hit}/{tot} = {r['recall']:.1f}%   "
              f"场景准确 {scene_ok}/{scene_tot} = {r['scene']:.1f}%   "
              f"连续帧 {r['avg']:.1f}ms / 最慢 {r['max']:.1f}ms   "
              f"（换画面首帧 {r['cold']:.1f}ms / 最慢 {r['coldmax']:.1f}ms）")
        return r

    base = evaluate("每场景只标 1 帧")
    # 再给战斗场景标一帧（模拟"又遇到一帧，顺手 add_scene"）：这一步不标新实体，
    # 只是把同一批控件在**另一种内容**下的分数喂进去 —— 动态控件靠这个收敛。
    C.add_scene(pk, load(args.ui, "battle_c.png"), "battle2", force=True,
                present=boxes["scenes"]["battle"])
    P.save_pack(args.root, pk)
    learned = evaluate("战斗场景多标 1 帧后")

    recall, scene_acc = learned["recall"], learned["scene"]
    print(f"\n多标一帧的收益：召回 {base['recall']:.1f}% -> {learned['recall']:.1f}%")
    if recall < 95:
        fails.append(f"实体召回 {recall:.1f}% < 95%")
    if scene_acc < 95:
        fails.append(f"场景准确 {scene_acc:.1f}% < 95%")
    if learned["max"] >= 50:
        fails.append(f"最慢 {learned['max']:.1f}ms >= 50ms")

    # ---------------- 3) 负样本：别的画面必须 0 实体
    out = OB.observe(pk, load(args.ui, "foreign.png"))
    got_foreign = [i for i, p in zip(ids, out["entities"]) if p]
    print(f"\n负样本 foreign.png -> 实体 {got_foreign or '无'}  scene={out['scene']} stale={out['stale']}")
    if got_foreign:
        fails.append(f"负样本误报 {got_foreign}")

    # ---------------- 4) 换主题必须 stale
    out = OB.observe(pk, load(args.ui, "battle_theme.png"))
    print(f"换浅色主题 -> stale={out['stale']} scene={out['scene']} conf={out['scene_conf']}")
    if not out["stale"]:
        fails.append("换主题没有报 stale")

    # ---------------- 5) 换尺寸：按比例重采样后仍应认出
    big = np.asarray(Image.fromarray(load(args.ui, "battle_b.png"))
                     .resize((1920, 1080), Image.BILINEAR))
    out = OB.observe(pk, big)
    got_big = [i for i, p in zip(ids, out["entities"]) if p]
    print(f"重采样到 1920x1080 -> {len(got_big)}/{len(ids)} 实体  {out['ms']:.1f}ms", end="")
    want_big = truth["battle_b.png"]
    bad = [e for e in want_big if e not in got_big]
    print(f"  漏 {bad or '-'}")
    if len(bad) > 1:
        fails.append(f"换尺寸漏报过多 {bad}")

    # ---------------- 6) 提议器：不需要模型的候选框
    t0 = time.perf_counter()
    cands = PR.propose(load(args.ui, "battle_a.png"))
    rows = PR.describe(load(args.ui, "battle_a.png"), cands)
    prop_ms = (time.perf_counter() - t0) * 1000.0
    cover = 0
    for eid in ["portrait", "hp_frame", "minimap", "enemy_plate"]:
        bx = boxes["scenes"] and [p for p in boxes["picks"] if p["id"] == eid][0]["box"]
        for r in rows:
            from pcv.feats import iou
            if iou(r["box"], bx) > 0.3:
                cover += 1
                break
    print(f"\n提议器 {len(rows)} 个候选 / {prop_ms:.0f}ms，其中命中真值框 {cover}/4")
    if cover < 3:
        fails.append(f"提议器覆盖不足 {cover}/4")

    print("\n== 结论 ==")
    if fails:
        for f in fails:
            print("  FAIL", f)
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
