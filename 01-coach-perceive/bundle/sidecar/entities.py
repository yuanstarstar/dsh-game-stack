"""实体层：把一帧游戏画面折成「实体 + 编号 + 位置 + 数值 + 描述」。

思路（研究结论可落地的最小版本）：
1. 文字实体：OCR 行按「同一行、间隙小」聚成块——按钮、卡牌名、数值、正文都是这样出来的。
2. 编号稳定：实体 key = kind|归一化文本；同一个 key 在跨帧里永远拿同一个编号（首次出现按阅读顺序分配）。
   这比像素差分有用得多：回合游戏里「变了什么」通常是「某个实体数值变了 / 新出现一个选项」。
3. 位置：每个实体给 bbox（框内坐标）和 screenBox（屏幕绝对坐标，可直接点击）。
4. 数值：正则抽 1/3、1~4、+15 这类；抽不到就留空，不猜。
5. 描述：先用规则表（按游戏存档的词典），认不出的图标才走一次视觉模型并缓存——不重复花钱。
"""
from __future__ import annotations

import hashlib
import json
import os
import re

BUTTON_WORDS = ("确定", "确认", "取消", "退出", "返回", "菜单", "继续", "开始", "选择", "关闭", "购买", "出售", "结束")
STATE_WORDS = ("体力", "血量", "生命", "混乱", "费用", "速度", "回合", "行动", "情感", "光", "金币", "经验")
NUM_RE = re.compile(r"(\d+(?:\s*[~～\-–]\s*\d+)?(?:\s*[/／]\s*\d+)?)")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def group_lines(lines: list[dict], gap: int = 14, max_w: int = 0) -> list[dict]:
    """同一水平带、水平间隙小的 OCR 行 → 一个文本块。

    gap 只管「同一行内的字距」；两块之间如果隔得远（比如左右两栏），必须断开，
    否则会把左栏「皮特之页」和右栏「总类层指定司书之页」粘成一条。
    """
    lines = [ln for ln in lines if ln.get("text")]
    if not lines:
        return []
    lines.sort(key=lambda l: (l["y"], l["x"]))
    groups: list[dict] = []
    for ln in lines:
        placed = False
        for g in groups:
            overlap = min(g["y1"], ln["y"] + ln["h"]) - max(g["y0"], ln["y"])
            same_band = overlap >= min(g["y1"] - g["y0"], ln["h"]) * 0.5
            near = ln["x"] - g["x1"] <= gap
            fits = (not max_w) or (max(g["x1"], ln["x"] + ln["w"]) - g["x0"] <= max_w)
            if same_band and near and fits:
                g["lines"].append(ln)
                g["x1"] = max(g["x1"], ln["x"] + ln["w"])
                g["y0"] = min(g["y0"], ln["y"])
                g["y1"] = max(g["y1"], ln["y"] + ln["h"])
                placed = True
                break
        if not placed:
            groups.append({"lines": [ln], "x0": ln["x"], "x1": ln["x"] + ln["w"], "y0": ln["y"], "y1": ln["y"] + ln["h"]})
    out = []
    for g in groups:
        parts = [ln["text"] for ln in sorted(g["lines"], key=lambda l: l["x"])]
        text = " ".join(parts).strip()
        out.append(
            {
                "text": text,
                "x": g["x0"],
                "y": g["y0"],
                "w": g["x1"] - g["x0"],
                "h": g["y1"] - g["y0"],
                "n": len(g["lines"]),
            }
        )
    return out


def classify(block: dict, frame_w: int, frame_h: int) -> str:
    t = _norm(block["text"])
    if any(w in t for w in BUTTON_WORDS) and len(t) <= 12:
        return "button"
    if any(w in t for w in STATE_WORDS):
        return "stat"
    if re.fullmatch(r"[\d\s/／:：.,%~～\-+]+", t):
        return "number"
    if block["y"] < frame_h * 0.12 and block["w"] > frame_w * 0.25:
        return "title"
    if block["y"] > frame_h * 0.86 and len(t) <= 10:
        return "hint"
    if len(t) >= 18:
        return "text"
    return "label"


def extract_value(text: str) -> str:
    """只认「有上下文的数字」：1/3、1~4、+15 这种。一行里数字太多就当纯文本，不瞎猜。"""
    m = NUM_RE.search(text)
    if not m:
        return ""
    if len(NUM_RE.findall(text)) > 2:
        return ""
    return _norm(m.group(1))


def _slot(n: int, edge: int = 48) -> int:
    """位置分桶：同一个位置附近的同类实体算「同一个实体」，跨帧编号才稳定。"""
    return n // edge


def build_entities(
    blocks: list[dict],
    frame_w: int,
    frame_h: int,
    origin: tuple[int, int] = (0, 0),
    state: dict | None = None,
    max_entities: int = 60,
) -> tuple[list[dict], dict]:
    """返回 (实体列表, 新状态)。state 用来保持编号稳定。

    编号规则：identity = kind|文本|位置桶。同文本出现在不同位置是**不同实体**（两个「致命」各占一个号），
    同一个实体换帧还在原位置则沿用原编号。
    """
    state = dict(state or {})
    by_id = state.setdefault("ids", {})
    next_id = int(state.get("next", 1))
    ents = []
    for b in blocks:
        kind = classify(b, frame_w, frame_h)
        text = b["text"]
        sx, sy = b["x"] + origin[0], b["y"] + origin[1]
        identity = "%s|%s|%d|%d" % (kind, _norm(text)[:24], _slot(sx), _slot(sy))
        if identity not in by_id:
            by_id[identity] = next_id
            next_id += 1
        ents.append(
            {
                "id": by_id[identity],
                "identity": identity,
                "kind": kind,
                "text": text,
                "value": extract_value(text),
                "desc": "",
                "box": [b["x"], b["y"], b["w"], b["h"]],
                "screenBox": [sx, sy, b["w"], b["h"]],
                "sha": hashlib.sha1(text.encode("utf-8")).hexdigest()[:8],
            }
        )
    ents.sort(key=lambda e: (e["box"][1] // 24, e["box"][0]))
    state["next"] = next_id
    return ents[:max_entities], state


def som_overlay(pil, ents: list[dict]):
    """Set-of-Mark 风格：把编号画到框上，模型可以直接说「点 12」。"""
    from PIL import ImageDraw

    img = pil.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    for e in ents:
        x, y, w, h = e["box"]
        draw.rectangle([x, y, x + w, y + h], outline=(0, 255, 180), width=2)
        label = str(e["id"])
        draw.rectangle([x, max(0, y - 14), x + 8 * len(label) + 6, max(0, y - 14) + 14], fill=(0, 0, 0))
        draw.text((x + 3, max(0, y - 13)), label, fill=(0, 255, 180))
    return img
