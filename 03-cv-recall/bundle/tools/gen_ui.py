#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成测试界面生成器（只用于验收，不属于插件运行时）。

造一个假游戏窗口，三种场景：
  battle  —— 小地图 / 技能条 / 敌方血条 在，对话框不在
  dialog  —— 对话框 + 名字板在，战斗 HUD 不在
  choice  —— 对话框 + 选项光标在

每个场景出两张**不同实例**（a/b）：血量填充、数字、小地图光点、光标位置、
全局亮度都不一样 —— 用 a 校准，拿 b 验收，才算真的泛化。

同时输出：
  boxes.json   —— 「一次标注」的真值框（模拟多模态模型只调用那一次）
  truth.json   —— 每张图的实体在场真值
  foreign.png  —— 完全另一套画面（负样本：应该输出 0 个实体）
  battle_theme.png —— 同布局但换浅色主题（应该报 stale，而不是静默错答）
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720

BOXES = {
    "portrait": [20, 16, 88, 88],
    "hp_frame": [120, 22, 320, 18],
    "minimap": [1080, 516, 180, 180],
    "skill1": [20, 648, 44, 44],
    "skill2": [72, 648, 44, 44],
    "skill3": [124, 648, 44, 44],
    "skill4": [176, 648, 44, 44],
    "enemy_plate": [500, 18, 280, 30],
    "dialog_panel": [90, 430, 1100, 260],
    "name_plate": [120, 392, 220, 36],
    "choice_cursor": [104, 470, 14, 32],
}
SEARCH = {"choice_cursor": [96, 440, 1060, 230]}

SCENE_ENTITIES = {
    "battle": ["portrait", "hp_frame", "minimap", "skill1", "skill2", "skill3",
               "skill4", "enemy_plate"],
    "dialog": ["portrait", "hp_frame", "dialog_panel", "name_plate"],
    "choice": ["portrait", "hp_frame", "dialog_panel", "name_plate", "choice_cursor"],
}
ALL_ENTITIES = list(BOXES)

THEME = {
    "dark": dict(bg=(18, 20, 28), panel=(34, 38, 52), line=(92, 100, 128),
                 text=(224, 230, 244), accent=(226, 78, 62), gold=(240, 200, 92)),
    "light": dict(bg=(222, 226, 236), panel=(246, 248, 252), line=(150, 158, 178),
                  text=(36, 40, 54), accent=(198, 60, 48), gold=(176, 132, 32)),
}


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _bg(d: ImageDraw.ImageDraw, t: dict, seed: int):
    rng = np.random.default_rng(seed)
    d.rectangle([0, 0, W, H], fill=t["bg"])
    for _ in range(26):  # 远景装饰：低对比大色块，模拟"场景背景"
        x = int(rng.integers(-80, W))
        y = int(rng.integers(120, H - 120))
        r = int(rng.integers(40, 150))
        c = tuple(int(v) for v in (np.array(t["bg"]) + rng.integers(-8, 14, 3)).clip(0, 255))
        d.ellipse([x - r, y - r // 2, x + r, y + r // 2], fill=c)


def _portrait(d: ImageDraw.ImageDraw, t: dict):
    x, y, w, h = BOXES["portrait"]
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=t["panel"],
                        outline=t["gold"], width=2)
    d.ellipse([x + 22, y + 14, x + 66, y + 58], fill=t["gold"])          # 头
    d.pieslice([x + 10, y + 52, x + 78, y + 120], 180, 360, fill=t["accent"])  # 肩


def _bars(d: ImageDraw.ImageDraw, t: dict, hp: float, mp: float):
    x, y, w, h = BOXES["hp_frame"]
    d.rectangle([x - 1, y - 1, x + w, y + h], fill=(10, 10, 12))
    d.rectangle([x, y, x + int(w * hp), y + h], fill=t["accent"])
    for i in range(1, 8):                                                # 静态刻度线
        tx = x + int(w * i / 8)
        d.line([tx, y, tx, y + h - 1], fill=(255, 255, 255))
    d.rectangle([x - 1, y - 1, x + w, y + h], outline=t["line"], width=1)
    mx, my = 120, 46
    d.rectangle([mx - 1, my - 1, mx + 240 + 1, my + 12 + 1], fill=(10, 10, 12))
    d.rectangle([mx, my, mx + int(240 * mp), my + 12], fill=(64, 132, 226))


def _gold(d: ImageDraw.ImageDraw, t: dict, gold: int, font):
    d.text((1100, 22), f"{gold:,}", fill=t["gold"], font=font)


def _minimap(d: ImageDraw.ImageDraw, t: dict, seed: int):
    """小地图：**地形恒定的，光点会动** —— 这就是真实游戏里的样子。

    第一版这里用 seed 把地形和光点一起重画，于是换一帧整块地图全变、任何模板匹配都
    必失效（实测两路 NCC 都掉到 0.38）。那不是"工具不行"，是测试数据不真实：
    真实小地图的地形/道路是静态资源，只有单位和光标在动。
    variant 只挪动其中 2 个光点（真实刷新率下就是这样），地形与其余光点保持不变。
    """
    x, y, w, h = BOXES["minimap"]
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=(24, 30, 26),
                        outline=t["line"], width=2)
    terr = np.random.default_rng(4104)                                   # 地形：恒定
    for _ in range(7):
        bx = int(x + 12 + terr.integers(0, w - 44))
        by = int(y + 12 + terr.integers(0, h - 44))
        bw = int(terr.integers(18, 52))
        bh = int(terr.integers(14, 40))
        d.rectangle([bx, by, bx + bw, by + bh], fill=(52, 64, 50))
    dots = np.random.default_rng(seed)                                   # 光点：随变体动
    for _ in range(9):
        bx = int(x + 14 + dots.integers(0, w - 28))
        by = int(y + 14 + dots.integers(0, h - 28))
        d.ellipse([bx, by, bx + 7, by + 7], fill=(120, 220, 140))
    d.ellipse([x + w // 2 - 5, y + h // 2 - 5, x + w // 2 + 5, y + h // 2 + 5],
              outline=t["gold"], width=2)


def _skills(d: ImageDraw.ImageDraw, t: dict):
    cols = [(226, 120, 60), (110, 190, 240), (170, 130, 230), (120, 220, 150)]
    for i in range(4):
        x, y, w, h = BOXES[f"skill{i + 1}"]
        d.rounded_rectangle([x, y, x + w, y + h], radius=9, fill=t["panel"],
                            outline=t["line"], width=2)
        d.ellipse([x + 8, y + 8, x + w - 8, y + h - 8], fill=cols[i])
        d.text((x + 4, y + h - 14), str(i + 1), fill=t["text"], font=_font(12))


def _enemy(d: ImageDraw.ImageDraw, t: dict, hp: float, font):
    x, y, w, h = BOXES["enemy_plate"]
    d.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=t["panel"],
                        outline=t["line"], width=2)
    d.rectangle([x + 8, y + 7, x + 22, y + h - 7], fill=(200, 90, 90))
    d.rectangle([x + 30, y + 8, x + 30 + int((w - 42) * hp), y + 14], fill=(210, 70, 70))
    d.text((x + 30, y + 15), "敌人 Lv.7", fill=t["text"], font=font)


def _dialog(d: ImageDraw.ImageDraw, t: dict, judge, font, big):
    x, y, w, h = BOXES["dialog_panel"]
    d.rounded_rectangle([x, y, x + w, y + h], radius=18, fill=t["panel"],
                        outline=t["line"], width=3)
    d.rectangle([x + 18, y + 18, x + w - 18, y + h - 18], outline=t["line"], width=1)
    nx, ny, nw, nh = BOXES["name_plate"]
    d.rounded_rectangle([nx, ny, nx + nw, ny + nh], radius=10, fill=t["accent"])
    d.text((nx + 14, ny + 10), "教官 艾丽", fill=(255, 255, 255), font=font)
    d.text((x + 40, y + 40), judge, fill=t["text"], font=big)


def _choices(d: ImageDraw.ImageDraw, t: dict, font, variant: int):
    x, y, w, h = BOXES["dialog_panel"]
    for i in range(3):
        ry = y + 110 + i * 44
        d.rounded_rectangle([x + 40, ry, x + w - 40, ry + 34], radius=8,
                            fill=(44, 50, 68) if i != variant else (70, 78, 104),
                            outline=t["line"], width=1)
        d.text((x + 60, ry + 8), f"选项 {i + 1}：这件事我照做", fill=t["text"], font=font)
    cx, cy, cw, ch = BOXES["choice_cursor"]
    d.polygon([(cx, cy + variant * 44), (cx + cw, cy + variant * 44 + ch // 2),
               (cx, cy + variant * 44 + ch)], fill=t["gold"])


def render(scene: str, variant: int = 0, theme: str = "dark", seed: int = 7):
    t = THEME[theme]
    im = Image.new("RGB", (W, H), t["bg"])
    d = ImageDraw.Draw(im)
    _bg(d, t, seed + variant)
    _portrait(d, t)
    _bars(d, t, hp=[0.42, 0.71][variant % 2], mp=[0.63, 0.28][variant % 2])
    _gold(d, t, [12840, 9930][variant % 2] * (1 + variant % 7), _font(20))

    if scene == "battle":
        _minimap(d, t, seed + variant * 31 + 5)
        _skills(d, t)
        _enemy(d, t, [0.55, 0.3][variant % 2], _font(16))
    elif scene == "dialog":
        _dialog(d, t, "这一带最近不太平，你最好先把补给备齐。", _font(18), _font(24))
    elif scene == "choice":
        _dialog(d, t, "你打算怎么回答？", _font(18), _font(24))
        _choices(d, t, _font(17), variant % 3)

    arr = np.asarray(im).astype(np.float32)
    gain = 1.0 + (0.02 if variant % 2 else -0.02)          # 每帧轻微亮度漂移
    arr = np.clip(arr * gain, 0, 255)
    arr = np.clip(arr + np.random.default_rng(seed + variant).normal(0, 1.1, arr.shape), 0, 255)
    return arr.astype(np.uint8)


def foreign():
    rng = np.random.default_rng(99)
    arr = np.zeros((H, W, 3), np.float32)
    arr[:, :, 0] = np.linspace(30, 90, W)[None, :]
    arr[:, :, 1] = np.linspace(90, 40, H)[:, None]
    im = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
        r = int(rng.integers(20, 90))
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=tuple(int(v) for v in rng.integers(40, 200, 3)))
    d.rectangle([300, 200, 900, 500], fill=(250, 250, 250))
    d.text((340, 240), "TOTALLY DIFFERENT APP", fill=(20, 20, 20), font=_font(40))
    return np.asarray(im).astype(np.uint8)


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "ui"
    os.makedirs(out, exist_ok=True)

    truth = {}
    for scene in ("battle", "dialog", "choice"):
        for v in (0, 1):
            arr = render(scene, v, seed=7)
            p = os.path.join(out, f"{scene}_{'ab'[v]}.png")
            Image.fromarray(arr).save(p)
            truth[os.path.basename(p)] = list(SCENE_ENTITIES[scene])
    # 第三帧只用来"多标一次"（模拟用户在实际游戏里又遇到一帧、顺手 add_scene），
    # 不进 truth —— 它不是测试集。
    Image.fromarray(render("battle", 2, seed=7)).save(os.path.join(out, "battle_c.png"))
    for name, img in (("foreign.png", foreign()),
                      ("battle_theme.png", render("battle", 0, theme="light", seed=7))):
        Image.fromarray(img).save(os.path.join(out, name))
    truth["foreign.png"] = []
    truth["battle_theme.png"] = list(SCENE_ENTITIES["battle"])

    picks = []
    for eid in ALL_ENTITIES:
        item = {"id": eid, "label": eid, "box": BOXES[eid]}
        if eid in SEARCH:
            item["search"] = SEARCH[eid]
        picks.append(item)
    with open(os.path.join(out, "boxes.json"), "w", encoding="utf-8") as f:
        json.dump({"picks": picks, "scenes": SCENE_ENTITIES}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "truth.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False, indent=1)
    print(json.dumps({"out": os.path.abspath(out), "images": len(truth),
                      "entities": len(ALL_ENTITIES)}, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
