# -*- coding: utf-8 -*-
"""把 cv_recall 的 JSON 画回图上：绿框=认出，红框=判为不存在。"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sidecar"))
from pcv import observe as O          # noqa: E402
from pcv import pack as P             # noqa: E402

DIR = r"C:\Users\<你的用户名>\Desktop\测试游戏图片"
OUT = os.path.join(DIR, "标注结果")
ROOT = os.path.expanduser("~/.dsh/cv-recall")
PACK = sys.argv[1] if len(sys.argv) > 1 else "isaac"

GREEN = (0, 255, 80)
RED = (255, 60, 60)
WHITE = (255, 255, 255)
DARK = (0, 0, 0)


def font(sz):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\consola.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()


def main():
    pk = P.load_pack(ROOT, PACK)
    os.makedirs(OUT, exist_ok=True)
    names = ["参考图.png"] + sorted(
        f for f in os.listdir(DIR) if f.lower().endswith(".png") and f != "参考图.png")
    tiles = []
    allr = []
    for name in names:
        rgb = np.asarray(Image.open(os.path.join(DIR, name)).convert("RGB"), dtype=np.uint8)
        r = O.observe(pk, rgb, {"detail": True, "boxes": True, "values": True, "labels": True})
        allr.append({"image": name, **r})
        labels = r.get("labels") or {}
        values = r.get("values") or {}
        im = Image.open(os.path.join(DIR, name)).convert("RGB")
        d = ImageDraw.Draw(im)
        fs = 30
        f = font(fs)
        # 顶部信息条
        head = (f"{r['scene']}  conf={r['scene_conf']}  dist={r['scene_dist']}  "
                f"{r['ms']}ms  changed={r['changed']}  stale={r['stale']}")
        d.rectangle([0, 0, im.width, 46], fill=(0, 0, 0))
        d.text((12, 8), head, font=f, fill=(200, 220, 255))
        n_on = 0
        drawn = []
        for det in r["detail"]:
            box = det.get("box")
            nm = det["id"]
            if box:
                n_on += 1
                x, y, w, h = [int(v) for v in box]
                d.rectangle([x, y, x + w, y + h], outline=GREEN, width=4)
                sub = det.get("sub") or {}
                nm = labels.get(det["id"]) or det["id"]
                val = values.get(det["id"])
                tag = f"{nm}" + ("" if val is None else f" = {val}")
                if w >= 120:
                    tag += (f"  g{sub.get('ncc_g', 0):.2f}/e{sub.get('ncc_e', 0):.2f}"
                            f"/t{sub.get('ncc_t', 0):.2f}")
                tw = int(f.getlength(tag)) + 10
                # 标签默认压在框上方（框挨得近时不会互相覆盖），顶到信息条才放到框下面
                if y - fs - 12 > 48:
                    tx, ty = x, y - fs - 10
                else:
                    tx, ty = x, y + h + 4
                tx = min(tx, im.width - tw - 4)
                # 和已画过的标签重叠就往上顶一行（心这类并排小图标必须这样，否则互相压字）
                for _ in range(4):
                    if not any(not (tx + tw < a or tx > c or ty + fs + 6 < b or ty > dd2)
                               for a, b, c, dd2 in drawn):
                        break
                    ty -= fs + 8
                    if ty < 48:
                        ty = 48
                        break
                drawn.append((tx, ty, tx + tw, ty + fs + 6))
                d.rectangle([tx, ty, tx + tw, ty + fs + 6], fill=DARK)
                d.text((tx + 5, ty + 2), tag, font=f, fill=GREEN)
            else:
                nom = next((e["box"] for e in pk["entities"] if e["id"] == nm), None)
                if nom:
                    x, y, w, h = [int(v) for v in nom]
                    d.rectangle([x, y, x + w, y + h], outline=RED, width=3)
                    tag = f"{labels.get(nm) or nm} ✗ 不存在"
                    d.rectangle([x, y, x + int(f.getlength(tag)) + 10, y + fs + 6], fill=DARK)
                    d.text((x + 5, y + 2), tag, font=f, fill=RED)
        # 左下角数值条：这才是「数值 + 描述」在画面上的样子
        if values:
            vtxt = "  ".join(f"{labels.get(k) or k} {v}" for k, v in values.items())
            vw = int(f.getlength(vtxt)) + 20
            d.rectangle([0, im.height - 50, vw, im.height], fill=(0, 0, 0))
            d.text((10, im.height - 46), vtxt, font=f, fill=(120, 240, 255))
        # 右下角汇总
        tail = f"{n_on}/{len(r['detail'])} 个实体"
        tw = int(f.getlength(tail))
        d.rectangle([im.width - tw - 24, im.height - 50, im.width, im.height], fill=(0, 0, 0))
        d.text((im.width - tw - 12, im.height - 46), tail, font=f, fill=(255, 255, 120))
        flat = "ref" if name == "参考图.png" else name[:8]
        path = os.path.join(OUT, f"{flat}.png")
        im.save(path)
        tiles.append(im.resize((1180, 737), Image.LANCZOS))
        print(f"{name[:14]:<16} -> {path}   {n_on}/{len(r['detail'])}")

    rows = (len(tiles) + 1) // 2
    sheet = Image.new("RGB", (1180 * 2, (737 + 8) * rows), (40, 40, 40))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % 2) * 1180, (i // 2) * (737 + 8)))
    sp = os.path.join(OUT, "汇总.png")
    sheet.save(sp)
    import json
    jp = os.path.join(OUT, "recall.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(allr, f, ensure_ascii=False, indent=1)
    print("汇总 ->", sp)
    print("json ->", jp)


if __name__ == "__main__":
    main()
