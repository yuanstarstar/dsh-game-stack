#!/usr/bin/env python3
"""coachctl —— 命令行操作游戏（不依赖 DSH 界面）

用法示例：
  python coachctl.py now                 # 看一眼：场景 + 变化块 + 关键文字（~200 tokens）
  python coachctl.py snap --ocr          # 完整快照 + OCR
  python coachctl.py diff                # 只回变化块
  python coachctl.py drive space 20      # 一步顶一串：按 20 次空格，自动停
  python coachctl.py key space           # 按一次键
  python coachctl.py click 812 500       # 点屏幕绝对坐标
  python coachctl.py click --text 确定    # 按文字点：自动找到「确定」并点它
  python coachctl.py anchor 废墟图书馆    # 锚定窗口（按标题片段）
  python coachctl.py cover               # 看当前锚定窗口是否被遮挡
  python coachctl.py windows             # 列出可见窗口

sidecar 没起时会自动拉起（默认 127.0.0.1:7799）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST = os.environ.get("COACH_HOST", "127.0.0.1")
PORT = int(os.environ.get("COACH_PORT", "7799"))
BASE = f"http://{HOST}:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))


def call(path: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok", False):
        raise SystemExit("失败：" + str(body.get("error")) + "\n" + str(body.get("trace") or ""))
    return body


def healthy() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=1.5) as resp:
            return b'"ok": true' in resp.read() or b'"ok":true' in resp.read()
    except Exception:  # noqa: BLE001
        return False


def ensure_sidecar() -> None:
    if healthy():
        return
    py = sys.executable
    out = os.path.join(os.environ.get("TEMP") or ".", "coachctl-sidecar.out")
    err = out.replace(".out", ".err")
    subprocess.Popen(
        [py, os.path.join(HERE, "coachd.py"), "--port", str(PORT), "--fps", "8", "--tile", "32"],
        cwd=HERE,
        stdout=open(out, "ab"),
        stderr=open(err, "ab"),
        creationflags=0x00000008 if os.name == "nt" else 0,
    )
    for _ in range(40):
        time.sleep(0.25)
        if healthy():
            return
    raise SystemExit("sidecar 起不来，看日志：" + err)


def print_lines(lines: list[dict], limit: int = 12) -> None:
    for ln in lines[:limit]:
        print(f"  [{ln.get('x')},{ln.get('y')}] {ln.get('text')}")


def main() -> None:
    try:  # 让中文在 Windows 控制台/管道里都别乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="命令行操作游戏（走 coachd sidecar）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("now", help="一眼看盘：场景+变化块+文字")
    p_snap = sub.add_parser("snap", help="快照")
    p_snap.add_argument("--ocr", action="store_true")
    p_snap.add_argument("--scale", type=float, default=2.0, help="OCR 放大倍数（小字认不全就调到 2~3）")
    p_snap.add_argument("--region", help='[x,y,w,h] / full / foreground / window:关键词')
    sub.add_parser("diff", help="只看变化块")
    p_drive = sub.add_parser("drive", help="一步顶一串")
    p_drive.add_argument("key", nargs="?", default="space")
    p_drive.add_argument("steps", nargs="?", type=int, default=20)
    p_key = sub.add_parser("key", help="按一次键")
    p_key.add_argument("key")
    p_click = sub.add_parser("click", help="点击（坐标或文字）")
    p_click.add_argument("pos", nargs="*")
    p_click.add_argument("--text")
    p_ct = sub.add_parser("click-text", help="按文字点击（比写坐标稳）")
    p_ct.add_argument("text")
    p_en = sub.add_parser("entities", help="画面 → 实体表（编号/类型/文字/数值/位置）")
    p_en.add_argument("--image", help="离线测试：指定一张 PNG，不抓活画面")
    p_en.add_argument("--som", action="store_true", help="另存一张带编号的标注图")
    p_en.add_argument("--json", action="store_true", help="输出完整 JSON")
    p_anchor = sub.add_parser("anchor", help="锚定窗口")
    p_anchor.add_argument("title")
    sub.add_parser("cover", help="看是否被遮挡")
    sub.add_parser("windows", help="列出窗口")
    sub.add_parser("health")

    args = ap.parse_args()
    ensure_sidecar()

    if args.cmd == "health":
        print(json.dumps(call("/health"), ensure_ascii=False))
        return
    if args.cmd == "anchor":
        r = call("/anchor", {"kind": "window", "title": args.title})
        print(json.dumps({"锚定": r.get("anchor")}, ensure_ascii=False))
        return

    if args.cmd == "windows":
        r = call("/windows")
        for w in r.get("windows", [])[:20]:
            print(f"  {w.get('hwnd')}  {w.get('title')}  {w.get('rect')}")
        return

    if args.cmd == "cover":
        r = call("/snapshot")
        s = r.get("snapshot", {})
        print(json.dumps({"anchor": s.get("anchor"), "region": s.get("region"), "coveredBy": s.get("coveredBy")}, ensure_ascii=False))
        return

    if args.cmd == "now":
        r = call("/now", {"ocr": True, "maxLines": 10})
        ch = r.get("changed") or {}
        print(f"scene={r.get('scene')} source={r.get('source')} 变化格={ch.get('count')} 区域={ch.get('total')} ocr={r.get('ocrMs')}ms")
        if ch.get("boxes"):
            print("  变化区域：" + " ".join(str(b) for b in ch["boxes"]))
        print_lines(r.get("lines") or [])
        return

    if args.cmd == "snap":
        body = {"ocr": bool(args.ocr), "ocrScale": args.scale}
        if args.region:
            body["region"] = args.region
        r = call("/snapshot", body)
        s = r.get("snapshot", {})
        print(f"region={s.get('region')} source={s.get('source')} coveredBy={s.get('coveredBy')}")
        if args.ocr:
            print_lines(s.get("ocr") or [])
        return

    if args.cmd == "diff":
        r = call("/diff")
        if r.get("first"):
            print("首次比较，已建基线")
        else:
            print(f"变化块={r.get('count')} 区域={r.get('changed')}")
        return

    if args.cmd == "key":
        print(json.dumps(call("/input/key", {"key": args.key}), ensure_ascii=False))
        return

    if args.cmd == "drive":
        t0 = time.time()
        r = call("/drive", {"key": args.key, "maxSteps": args.steps, "intervalMs": 450})
        print(f"按了 {r.get('pressed')} 次，停在 {r.get('stoppedBy')}，场景 {r.get('scene')}，{int((time.time()-t0)*1000)}ms")
        print_lines(r.get("lines") or [], 8)
        return

    if args.cmd == "entities":
        body = {"ocrScale": 2.0}
        if args.image:
            body["imagePath"] = args.image
        if args.som:
            body["som"] = True
        r = call("/entities", body)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
            return
        print(f"场景={r.get('scene')} 来源={r.get('source')} 实体={r.get('count')} 区域={r.get('region')}")
        for e in r.get("entities") or []:
            box = e["box"]
            val = f" 值={e['value']}" if e.get("value") else ""
            print(f"  #{e['id']:<3} {e['kind']:<7} [{box[0]:>4},{box[1]:>4},{box[2]:>3},{box[3]:>3}] {e['text']}{val}")
        ch = r.get("changed") or {}
        if ch.get("new") or ch.get("gone") or ch.get("valueChanged"):
            print(f"  变化：新 {ch.get('new')} 消失 {ch.get('gone')} 数值变 {ch.get('valueChanged')}")
        if r.get("somPath"):
            print(f"  标注图：{r['somPath']}")
        return

    if args.cmd == "click-text":
        r = call("/click_text", {"text": args.text})
        c = r["clicked"]
        print(f"点了「{c['text']}」@({c['x']},{c['y']})，候选 {r['matches']} 个")
        return

    if args.cmd == "click":
        if args.text:
            r = call("/click_text", {"text": args.text})
            print(f"点了「{r['clicked']['text']}」@({r['clicked']['x']},{r['clicked']['y']})，候选 {r['matches']} 个")
        else:
            if len(args.pos) != 2:
                raise SystemExit("用法：click X Y  或  click --text 文字")
            x, y = int(args.pos[0]), int(args.pos[1])
            call("/input/click", {"x": x, "y": y})
            print(f"点了 ({x},{y})")
        return


if __name__ == "__main__":
    main()
