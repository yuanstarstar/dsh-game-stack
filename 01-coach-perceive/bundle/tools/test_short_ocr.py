"""短文本（1~2 字）OCR 回归测试。

背景：WinRT OCR 对孤立单字几乎不出结果，旧代码还在两处"顺手"把短行丢掉，
导致「是 / 否 / 敌 / 确定」这类按钮从画面上"消失"。这个测试锁死三件事：
  1. 两字文本常规路径必须读得到；
  2. 单字在常规路径读空时，ocr_rescue（放大 + 拼版投票）必须能救回来；
  3. 抢救结果必须带 weak 标记，调用方不能当硬证据用。

跑法（插件目录下）：
  python tools/test_short_ocr.py
需要 Microsoft YaHei 字体（Windows 自带）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'sidecar'))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import coachd  # noqa: E402

FONT = r'C:\Windows\Fonts\msyh.ttc'

SHORT_OK = ['是', '否', '敌']
TWO_CHAR = ['确定', '自动', '跳过']
LONG_CHUNK = '结束回合'


def render(txt, px=30, pad=4):
    f = ImageFont.truetype(FONT, px)
    box = f.getbbox(txt)
    w = max(8, box[2] - box[0]) + pad * 2
    h = max(8, box[3] - box[1]) + pad * 2
    img = Image.new('RGB', (w, h), (26, 28, 34))
    ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), txt, font=f, fill=(236, 236, 236))
    return img


def plain(img):
    return [x['text'] for x in coachd.normalize_lines(coachd.ocr_pil(img, 2.0))]


def rescue(img):
    r = coachd.ocr_rescue(img)
    return r, [x['text'] for x in coachd.normalize_lines(r)]


fails = []


def check(cond, msg):
    if cond:
        print('  ok   %s' % msg)
    else:
        print('  FAIL %s' % msg)
        fails.append(msg)


print('[1] 两字词：常规 OCR 应读到')
for s in TWO_CHAR:
    got = plain(render(s))
    check(any(s in t for t in got), '%s -> %s' % (s, got))

print('[2] 单字：常规读空，抢救应救回')
for s in SHORT_OK:
    base = plain(render(s))
    r, got = rescue(render(s))
    check(any(s in t for t in got), '%s 常规=%s 抢救=%s' % (s, base, got))
    check(r.get('weak') is True, '%s 抢救结果必须 weak=True' % s)

print('[3] 长一点的整行不该被抢救破坏')
r, got = rescue(render(LONG_CHUNK))
check(any(LONG_CHUNK in t for t in got), '%s -> %s' % (LONG_CHUNK, got))

print('[4] 已知引擎限制：单个数字/字母 抢救也可能读空（不算失败，只记录）')
for s in ('1',):
    r, got = rescue(render(s))
    print('  note %s -> %s votes=%s' % (s, got, r.get('votes')))

print()
if fails:
    print('失败 %d 项：' % len(fails))
    for f in fails:
        print(' -', f)
    sys.exit(1)
print('短文本 OCR 全部通过 ✅')
