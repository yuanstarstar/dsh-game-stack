# -*- coding: utf-8 -*-
"""文字通道验收：系统 OCR 读普通文字，读不出**必须留空**（不许编）。

三组用例：
1. 合成中文对话框（正常 UI 文字）——必须逐块读对、分得出 button/stat、取出数值。
2. 《以撒》真实截图（像素字/花体）——必须**读不出**（返回空 + 说明），因为这类字
   系统 OCR 只会吐"看着像字"的胡话。
3. 数字字段（金币/炸弹/钥匙计数）——指定 alphabet="digits" 时走内置字形模型，必须读对。

跑法：python tools\\text_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "sidecar"))

from pcv import text as T                                  # noqa: E402

REAL = r"C:\Users\<你的用户名>\Desktop\测试游戏图片\参考图.png"
FONT = r"C:\Windows\Fonts\msyh.ttc"
PY = sys.executable


def make_dialog(path: str) -> None:
    img = Image.new("RGB", (1280, 720), (18, 20, 26))
    d = ImageDraw.Draw(img)
    f1 = ImageFont.truetype(FONT, 40)
    f2 = ImageFont.truetype(FONT, 28)
    d.rectangle([80, 430, 1200, 660], fill=(30, 34, 44), outline=(120, 150, 210), width=3)
    d.text((120, 460), "巨响从走廊深处传来，你握紧了武器。", font=f2, fill=(235, 238, 245))
    d.text((120, 510), "生命：82 / 100", font=f2, fill=(200, 220, 200))
    d.text((120, 570), "继续", font=f1, fill=(255, 225, 120))
    d.text((420, 570), "离开", font=f1, fill=(200, 200, 205))
    img.save(path)


def local_checks(results: list) -> None:
    print("== 纯函数的规矩 ==")
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(("  PASS " if good else "  FAIL ") + f"{name}: {got!r}" + ("" if good else f"（期望 {want!r}）"))

    chk("汉字间空格要去掉", T.clean_text("继 续 冒 险"), "继续冒险")
    chk("英文词间空格要留着", T.clean_text("Open World"), "Open World")
    chk("标点两侧空白收掉", T.clean_text("体力 ： 82 / 100"), "体力：82/100")
    chk("一个数才取", T.value_of("生命：82/100"), "82/100")
    chk("多个数不猜", T.value_of("2 ~ 3 / 5"), None)
    chk("按钮", T.kind_of("继续", [0, 0, 10, 10], 1280, 720), "button")
    chk("数值行", T.kind_of("生命：82/100", [0, 0, 10, 10], 1280, 720), "stat")
    chk("纯数字", T.kind_of("1234", [0, 0, 10, 10], 1280, 720), "number")
    # 同一位置同一句话要给同一个 id（连续帧才对得上）
    a = T.stable_id("button", "继续", [100, 200, 80, 40])
    b = T.stable_id("button", "继 续", [102, 201, 80, 40])
    chk("稳定 id", a == b, True)
    # 行→块：同一行的两段要合并，隔很远的另一栏不能粘进来
    lines = [{"text": "确定", "x": 100, "y": 300, "w": 70, "h": 36},
             {"text": "取消", "x": 200, "y": 302, "w": 70, "h": 36},
             {"text": "右栏", "x": 900, "y": 300, "w": 70, "h": 36}]
    blocks = T.group_lines(lines, gap=40, max_w=600)
    chk("行→块 段数", len(blocks), 2)
    chk("行→块 合并文本", blocks[0]["text"] if blocks else "", "确定取消")
    print("   （纯函数）" + ("全过" if ok else "有失败"))
    results.append(("纯函数", ok))


def http_checks(results: list) -> None:
    p = subprocess.Popen([PY, os.path.join(HERE, "sidecar", "cvcd.py"), "--port", "0"],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                         stderr=subprocess.DEVNULL)
    port = json.loads(p.stdout.readline())["port"]

    def post(path, body, timeout=180):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        cn = os.path.join(HERE, ".tmp-texttest.png")
        make_dialog(cn)
        t0 = time.time()
        r = post("/text", {"path": cn})
        cold = (time.time() - t0) * 1000
        post("/text", {"path": cn})                      # 预热常驻 worker
        t0 = time.time()
        r = post("/text", {"path": cn})
        hot = (time.time() - t0) * 1000
        print(f"== 合成中文对话框（下半屏）==  冷 {cold:.0f}ms / 热 {hot:.0f}ms")
        blocks = r.get("blocks") or []
        for b in blocks:
            print(f"   {b['id']:>3} {b['kind']:<6} “{b['text']}”"
                  f"{'' if 'value' not in b else ' = ' + str(b['value'])}  {b['box']}")
        joined = r.get("text") or ""
        want = ["走廊", "生命：82/100", "继续", "离开"]
        good = all(w in joined for w in want)
        print(("  PASS" if good else "  FAIL") + f" 读出 4 段：{joined!r}")
        kinds = {b["text"]: b["kind"] for b in blocks}
        good2 = kinds.get("继续") == "button" and kinds.get("离开") == "button" \
            and kinds.get("生命：82/100") == "stat"
        print(("  PASS" if good2 else "  FAIL") + f" 分类正确：{kinds}")
        results.append(("合成中文", good and good2))

        if os.path.isfile(REAL):
            print("== 《以撒》真实截图（像素字）==")
            for band in ("bottom", "full"):
                t0 = time.time()
                r = post("/text", {"path": REAL, "band": band})
                ms = (time.time() - t0) * 1000
                txt = r.get("text") or ""
                good = txt == ""
                print(("  PASS" if good else "  FAIL")
                      + f" {band}: {ms:.0f}ms 读出 {txt!r}（像素字应当读不出）")
                if r.get("note"):
                    print(f"        说明：{r['note']}")
                results.append((f"像素字留空/{band}", good))
            t0 = time.time()
            r = post("/text", {"path": REAL, "box": [196, 290, 112, 72], "alphabet": "digits"})
            good = (r.get("text") or "") == "00"
            print(("  PASS" if good else "  FAIL")
                  + f" 金币计数走了字形模型：{r.get('text')!r}（{(time.time()-t0)*1000:.0f}ms, via {r.get('via')}）")
            results.append(("数字走字形模型", good))
        else:
            print(f"（跳过真实截图：找不到 {REAL}）")
    finally:
        p.terminate()
        try:
            os.unlink(os.path.join(HERE, ".tmp-texttest.png"))
        except OSError:
            pass


def main() -> int:
    results: list = []
    local_checks(results)
    http_checks(results)
    bad = [n for n, ok in results if not ok]
    print("\n== 结论 ==")
    if bad:
        print("  FAIL " + "、".join(bad))
        return 1
    print(f"  全部 {len(results)} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
