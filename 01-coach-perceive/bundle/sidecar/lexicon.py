"""可教的特征库（lexicon）：多模态模型教一次 → 本地纯 CV 秒级复现。

为什么是「特征」而不是「模型权重」：
- 多模态模型（DSH 自己）只负责**语义**：这块是司书卡、那块是体力值、描述怎么写。
- 语义落到本地就是一组可计算的**特征**：归一化位置、dHash 指纹、颜色直方图、边缘密度、
  框内 OCR 文字、属性相对偏移。这些都是几十微秒的运算，不需要再问模型。
- 于是：教一次（几千 token），之后每帧 5~15ms、0 token，且换游戏只是换一份 lexicon。

数据流：
  teach JSON（模型产出）──compile──> lexicon JSON（特征）──detect──> 实体表 + 值 + 描述
结构模板（layout: grid/cols/gap）让人只教一张卡，自动铺满一栏。
"""
from __future__ import annotations

import json
import os

import numpy as np
from PIL import Image

# ---------- 特征 ----------


def dhash(img: Image.Image, size: int = 8) -> str:
    g = img.convert("L").resize((size + 1, size), Image.BILINEAR)
    a = np.asarray(g, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return "%016x" % int("".join("1" if b else "0" for b in bits), 2)


def color_hist(img: Image.Image, bins: tuple[int, int, int] = (6, 4, 4)) -> list[float]:
    hsv = np.asarray(img.convert("HSV"), dtype=np.uint8)
    hist, _ = np.histogramdd(
        (hsv[..., 0].ravel(), hsv[..., 1].ravel(), hsv[..., 2].ravel()),
        bins=bins,
        range=((0, 256), (0, 256), (0, 256)),
    )
    hist = hist.flatten().astype(np.float32)
    total = float(hist.sum()) or 1.0
    return (hist / total).tolist()


def edge_density(img: Image.Image) -> float:
    a = np.asarray(img.convert("L").resize((48, 48), Image.BILINEAR), dtype=np.float32)
    gx = np.abs(np.diff(a, axis=1)).mean()
    gy = np.abs(np.diff(a, axis=0)).mean()
    return round(float((gx + gy) / 2.0), 3)


def mean_color(img: Image.Image) -> list[int]:
    return [int(v) for v in np.asarray(img.convert("RGB"), dtype=np.float32).reshape(-1, 3).mean(axis=0)]


def features(img: Image.Image) -> dict:
    return {"dhash": dhash(img), "hist": color_hist(img), "edge": edge_density(img), "mean": mean_color(img)}


def hist_sim(h1: list[float], h2: list[float]) -> float:
    """余弦相似度。注意分母必须是 L2 范数——用 sum() 会让直方图相似度虚低到 0.07。"""
    a, b = np.asarray(h1, np.float32), np.asarray(h2, np.float32)
    denom = float(np.sqrt(float((a * a).sum()) * float((b * b).sum()))) or 1.0
    return float((a * b).sum() / denom)


def dhash_sim(h1: str, h2: str) -> float:
    try:
        d = bin(int(h1, 16) ^ int(h2, 16)).count("1")
    except ValueError:
        return 0.0
    return 1.0 - d / 64.0


# ---------- 教 → 编译 ----------


def _rect_px(rect: list[float], w: int, h: int) -> tuple[int, int, int, int]:
    x, y, rw, rh = rect
    return (int(x * w), int(y * h), max(1, int(rw * w)), max(1, int(rh * h)))


def compile_teach(teach: dict, image: Image.Image) -> dict:
    """把模型给的语义 JSON 编译成可本地计算的 lexicon。"""
    w, h = image.size
    lex = {
        "game": teach.get("game", ""),
        "screen": teach.get("screen", ""),
        "resolution": list(teach.get("resolution") or [w, h]),
        "templates": [],
    }
    for idx, ent in enumerate(teach.get("entities") or []):
        box = _rect_px(ent["bbox"], w, h)
        crop = image.crop((box[0], box[1], box[0] + box[2], box[1] + box[3]))
        tpl = {
            "id": int(ent.get("id") or idx + 1),  # 编号稳定：按教学顺序固定，重教不换号
            "name": ent.get("name", "未命名"),
            "kind": ent.get("kind", "label"),
            "rect": [round(v, 4) for v in ent["bbox"]],
            "desc": ent.get("desc", ""),
            "interactive": bool(ent.get("interactive")),
            "feat": features(crop),
            "props": [],
        }
        for prop in ent.get("props") or []:
            px, py, pw, ph = prop.get("bbox") or [0, 0, 0, 0]
            tpl["props"].append(
                {
                    "name": prop.get("name", "值"),
                    "rect": [round(px, 4), round(py, 4), round(pw, 4), round(ph, 4)],
                    "rel": [round(px - ent["bbox"][0], 4), round(py - ent["bbox"][1], 4)],
                    "reader": prop.get("reader") or {"type": "text"},
                    "sample": prop.get("value", ""),
                }
            )
        lex["templates"].append(tpl)
    return lex


def save_lexicon(path: str, lex: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(lex, fh, ensure_ascii=False, indent=1)


def load_lexicon(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------- 本地识别 ----------


def _lines_in(lines: list[dict], box: tuple[int, int, int, int], pad: int = 2) -> list[dict]:
    x, y, w, h = box
    out = []
    for ln in lines or []:
        cx, cy = ln["x"] + ln["w"] / 2, ln["y"] + ln["h"] / 2
        if x - pad <= cx <= x + w + pad and y - pad <= cy <= y + h + pad:
            out.append(ln)
    return out


def detect(lex: dict, image: Image.Image, lines: list[dict] | None = None, min_score: float = 0.55, search: int = 3) -> list[dict]:
    """纯本地：按 lexicon 里的特征把画面拆成实体。不调用任何模型。

    search>0 时在 ±search 像素内做小范围搜索——UI 会因为字体渲染、窗口边框、DPI 差 1~2px，
    对齐搜索能让「同一屏复用」的相似度从 0.6 回到 0.98，避免误判为“不认识”。
    """
    w, h = image.size
    out = []
    for tpl in lex.get("templates") or []:
        box = _rect_px(tpl["rect"], w, h)
        if box[2] < 3 or box[3] < 3:
            continue
        best = (-1.0, box, None)
        for dy in range(-search, search + 1):
            for dx in range(-search, search + 1):
                bx, by = box[0] + dx, box[1] + dy
                if bx < 0 or by < 0 or bx + box[2] > w or by + box[3] > h:
                    continue
                # 搜索阶段只算 dHash（一次 resize 到 9x8，很便宜）；命中后再补颜色特征
                crop = image.crop((bx, by, bx + box[2], by + box[3]))
                dsim = dhash_sim(tpl["feat"]["dhash"], dhash(crop))
                if dsim > best[0]:
                    best = (dsim, [bx, by, box[2], box[3]], None)
        dsim, box2, _feat = best
        crop2 = image.crop((box2[0], box2[1], box2[0] + box2[2], box2[1] + box2[3]))
        hsim = hist_sim(tpl["feat"]["hist"], color_hist(crop2))
        sim = 0.55 * dsim + 0.45 * hsim
        text = " ".join(ln["text"] for ln in _lines_in(lines or [], box2)).strip()
        ent = {
            "id": tpl.get("id", 0),
            "name": tpl["name"],
            "kind": tpl["kind"],
            "box": box2,
            "text": text,
            "score": round(sim, 3),
            "ok": sim >= min_score,
            "props": {},
        }
        for prop in tpl.get("props") or []:
            abs_rect = [
                tpl["rect"][0] + prop["rel"][0],
                tpl["rect"][1] + prop["rel"][1],
                prop["rect"][2],
                prop["rect"][3],
            ]
            pb = _rect_px(abs_rect, w, h)
            got = " ".join(ln["text"] for ln in _lines_in(lines or [], pb)).strip()
            ent["props"][prop["name"]] = got or prop.get("sample", "")
        out.append(ent)
    return out


def expand_grid(lex: dict, cols: int, gap_rel: float = 0.012) -> dict:
    """教一张卡 → 按等距列自动铺开（再靠 detect 的相似度逐张校验）。"""
    new = dict(lex)
    extra = []
    for tpl in lex.get("templates") or []:
        if not tpl.get("grid"):
            continue
        x, y, rw, rh = tpl["rect"]
        for i in range(1, max(1, cols)):
            nx = x + i * (rw + gap_rel)
            if nx + rw > 1.0:
                break
            copy = json.loads(json.dumps(tpl))
            copy["rect"] = [round(nx, 4), y, rw, rh]
            copy["name"] = f"{tpl['name']}#{i + 1}"
            extra.append(copy)
    new["templates"] = (lex.get("templates") or []) + extra
    return new


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="lexicon 编译器/识别器自测")
    ap.add_argument("--teach", help="模型产出的语义 JSON")
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", help="保存编译后的 lexicon")
    ap.add_argument("--detect", action="store_true")
    args = ap.parse_args()
    img = Image.open(args.image).convert("RGB")
    if args.teach:
        lex = compile_teach(json.load(open(args.teach, encoding="utf-8")), img)
        if args.out:
            save_lexicon(args.out, lex)
        print(json.dumps([{"name": t["name"], "rect": t["rect"], "props": [p["name"] for p in t["props"]]} for t in lex["templates"]], ensure_ascii=False, indent=1))
    if args.detect:
        lex = load_lexicon(args.out)
        for e in detect(lex, img):
            print(f"{'OK ' if e['ok'] else '?? '} {e['name']:<10} {e['score']:.2f}  props={e['props']} text={e['text'][:20]}")
