/**
 * @local/dsh-coach-perceive —— 游戏感知 + 操作桥
 *
 * 把 sidecar（Python: ffmpeg 常驻抓帧 + Pillow 差分 + Windows OCR）包装成模型可用的工具，
 * 目的是让「看清盘面」这件事不再等于「截一张图问一次模型」：
 *   - coach_diff  ：先问「变了没有」，没变就别再花 token；
 *   - coach_snapshot{ocr:true}：变化后只取增量信息（文字/数字），不传像素；
 *   - coach_frame{attach:true}：只有 OCR 不可靠时才真的给模型看一张图。
 *
 * 输入层默认关闭（cfg.inputEnabled=true 才启用）：SendInput 按键/点击、按文字点击、连续推进。
 */

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, statSync, mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'coach-perceive-v3'

const HERE = dirname(fileURLToPath(import.meta.url))
const SIDECAR = join(HERE, 'sidecar', 'coachd.py')
// ---- 服务端字段可能退化（数组变对象、null、缺字段），渲染前统一过一遍，避免 .map is not a function ----
const arr = (v, key) => (Array.isArray(v) ? v : (v && typeof v === 'object' && Array.isArray(v[key]) ? v[key] : []))
const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {})
const txt = (v, dflt = '') => (v == null ? dflt : String(v))
const px = (b) => (Array.isArray(b) && b.length >= 2 ? `(${b[0]},${b[1]})` : '?')

function renderState(d0) {
  const d = obj(d0)
  const sub = txt(d.sub, '?')
  const dice = arr(d.speedDice)
  const cards = arr(obj(d.hand).cards)
  const bars = arr(d.hp, 'bars')
  const texts = arr(d.hp, 'texts')
  const unk = arr(d.unknown)
  const ex = obj(d.executeButton)

  const diceTxt = dice.map((x) => {
    const o = obj(x)
    const side = o.side === 'self' ? '我方' : o.side === 'enemy' ? '敌方' : txt(o.side, '?')
    const val = o.value == null ? '' : ` 值${o.value}`
    return `  骰${txt(o.i, '?')} ${side} ${txt(o.color, '?')} @${px(o.screen)}${val}`
  }).join('\n')

  const byScreen = cards.slice().sort((a, b) => ((a?.screen?.[0] ?? 0) - (b?.screen?.[0] ?? 0)))
  const handTxt = (d.handVisible && cards.length)
    ? byScreen.map((c0) => {
        const c = obj(c0)
        const nm = c.name ? ` ${c.name}` : ''
        const dice2 = arr(c.dice).join('/')
        return `  [${txt(c.i, '?')}] @${px(c.screen)} ${arr(c.box).join('x')}${nm}${dice2 ? ` 骰 ${dice2}` : ''}`
      }).join('\n')
    : `  （没开）${txt(obj(d.hand).reason || d.handReason, '')}`

  const barTxt = bars.map((b0) => {
    const b = obj(b0)
    const pct = b.frac == null ? '?' : `${Math.round(Number(b.frac) * 100)}%`
    return `  ${txt(b.color, '?')} ${pct} @${arr(b.box).join(',')}`
  }).join('\n')

  const textTxt = texts.slice(0, 6).map((t) => `  「${txt(obj(t).text)}」@${px(obj(t).screen)}`).join('\n')

  const head = `场景 ${txt(d.sceneZh || d.scene, '未知')}/${sub}` +
    `｜手牌 ${d.handVisible ? `${cards.length} 张` : '未开'}` +
    `${d.autoRoll ? '｜自动掷骰 ON' : ''}${d.animating ? '｜⚠画面还在动' : ''}｜${txt(d.ms, '?')}ms`

  const exTxt = ex.found
    ? `「下令战斗」@${px(ex.screen)}（${txt(ex.how, '?')}）` +
      (arr(ex.candidates).length > 1 ? `，点不中可以依次试：${arr(ex.candidates).map(px).join(' ')}` : '')
    : `「下令战斗」没找到：${txt(ex.note, '坐标未知')}`

  const lines = [
    head,
    dice.length ? `速度骰 ${dice.length} 个：\n${diceTxt}` : '速度骰：一个都没认出来',
    `手牌：\n${handTxt}`,
    barTxt ? `血条：\n${barTxt}` : '',
    textTxt ? `底部文字（OCR 原文，可能读错）：\n${textTxt}` : '',
    exTxt,
    unk.length ? `读不出来：${unk.map((x) => txt(x)).join('、')}` : '',
    txt(d.hint),
  ].filter(Boolean)
  return lines.join('\n')
}

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string' } },
    required: ['text'],
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

/** coach_frame 需要能在同一份输出里混一张图：有图就渲染成图像块 + 文字说明。 */
const FRAME_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: true,
    properties: { text: { type: 'string' }, image: { type: 'object' } },
    required: ['text'],
  },
  render: (_args, value) => {
    const blocks = []
    if (value?.image) blocks.push({ type: 'image', attachment: value.image })
    blocks.push({ type: 'text', text: String(value?.text ?? '') })
    return blocks
  },
}

const DEFAULTS = { fps: 8, tile: 32, callTimeoutMs: 12000, inputEnabled: true }

// ---------------------------------------------------------------- python 解析

function dshHome() {
  return process.env.DSH_HOME || join(homedir(), '.dsh')
}

function findRuntimePython() {
  const base = join(dshHome(), 'dsh-runtimes')
  if (!existsSync(base)) return undefined
  let dirs = []
  try {
    dirs = readdirSync(base)
  } catch {
    return undefined
  }
  for (const d of dirs) {
    const p = join(base, d, 'dependencies', 'python', 'python.exe')
    if (existsSync(p)) return p
  }
  return undefined
}

function resolvePython(config) {
  const explicit = config?.python || process.env.DSH_COACH_PYTHON
  if (explicit) return explicit
  const runtime = findRuntimePython()
  if (runtime) return runtime
  return process.platform === 'win32' ? 'python' : 'python3'
}

// ---------------------------------------------------------------- sidecar 进程

class Sidecar {
  constructor(log, config) {
    this.log = log
    this.config = config
    this.proc = undefined
    this.port = 0
    this.stderr = ''
    this.starting = undefined
    this.python = resolvePython(config)
  }

  get base() {
    return `http://127.0.0.1:${this.port}`
  }

  async ensure() {
    if (this.port && this.proc && this.proc.exitCode === null) return this
    if (this.starting) return this.starting
    this.starting = this._start().finally(() => {
      this.starting = undefined
    })
    return this.starting
  }

  async _start() {
    if (!existsSync(SIDECAR)) throw new Error(`sidecar 脚本不存在：${SIDECAR}`)
    const args = [
      SIDECAR,
      '--port', '0',
      '--fps', String(this.config?.fps ?? DEFAULTS.fps),
      '--tile', String(this.config?.tile ?? DEFAULTS.tile),
    ]
    if (this.config?.obsPort) args.push('--obs-port', String(this.config.obsPort))
    if (this.config?.obsHost) args.push('--obs-host', String(this.config.obsHost))
    if (this.config?.obsPassword) args.push('--obs-password', String(this.config.obsPassword))
    this.log?.info?.(`启动感知 sidecar：${this.python} ${args.join(' ')}`)
    const proc = spawn(this.python, args, {
      cwd: HERE,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.proc = proc
    proc.stderr?.on('data', (chunk) => {
      const s = String(chunk)
      this.stderr = (this.stderr + s).slice(-4000)
    })
    const first = await new Promise((resolve, reject) => {
      let buf = ''
      const timer = setTimeout(() => reject(new Error(`sidecar 启动超时（6s）。stderr：${this.stderr.slice(-600)}`)), 6000)
      proc.stdout?.on('data', (chunk) => {
        buf += String(chunk)
        const idx = buf.indexOf('\n')
        if (idx >= 0) {
          clearTimeout(timer)
          resolve(buf.slice(0, idx).trim())
        }
      })
      proc.once('error', (err) => {
        clearTimeout(timer)
        reject(new Error(`无法启动 python（${this.python}）：${err.message}`))
      })
      proc.once('exit', (code) => {
        clearTimeout(timer)
        reject(new Error(`sidecar 启动即退出（code=${code}）。stderr：${this.stderr.slice(-600)}`))
      })
    })
    let info
    try {
      info = JSON.parse(first)
    } catch {
      throw new Error(`sidecar 启动输出无法解析：${String(first).slice(0, 300)}`)
    }
    this.port = info.port
    this.screen = info.screen
    this.log?.info?.(`感知 sidecar 就绪：端口 ${this.port}，屏幕 ${info.screen?.join('x')}`)
    return this
  }

  async call(path, body, timeoutMs = this.config?.callTimeoutMs ?? DEFAULTS.callTimeoutMs) {
    await this.ensure()
    const init = body === undefined
      ? { method: 'GET' }
      : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
    let res
    try {
      res = await fetch(this.base + path, { ...init, signal: AbortSignal.timeout(timeoutMs) })
    } catch (err) {
      throw new Error(`感知 sidecar 调用失败（${path}）：${err?.message ?? err}`)
    }
    const text = await res.text()
    let data
    try {
      data = text ? JSON.parse(text) : {}
    } catch {
      throw new Error(`感知 sidecar 返回非 JSON（${path}）：${text.slice(0, 300)}`)
    }
    if (!res.ok || data.ok === false) {
      throw new Error(`感知失败（${path}）：${data.error ?? text.slice(0, 300)}`)
    }
    return data
  }

  stop() {
    const proc = this.proc
    this.port = 0
    this.proc = undefined
    if (!proc) return
    try {
      proc.kill()
    } catch {
      /* 进程可能已退出 */
    }
  }
}

// ---------------------------------------------------------------- 区域解析

const WINDOW_CACHE_MS = 400
let windowCache = { at: 0, rows: [] }

async function listWindows(side) {
  if (Date.now() - windowCache.at < WINDOW_CACHE_MS) return windowCache.rows
  const data = await side.call('/windows')
  windowCache = { at: Date.now(), rows: data.windows ?? [] }
  return windowCache.rows
}

/**
 * region 语法：
 *   undefined / "anchor"           -> 锚定区域（没锚定=整屏；推荐先用 coach_anchor 锚一次）
 *   "full" / "screen"              -> 强制整屏
 *   "foreground"                   -> 当前前台窗口
 *   "window:文明"                   -> 标题包含该片段的窗口
 *   [x,y,w,h] / {x,y,w,h}          -> 绝对区域
 */
async function resolveRegion(side, spec) {
  if (spec === undefined || spec === null || spec === 'anchor') return 'anchor'
  if (spec === 'full' || spec === 'screen') return 'screen'
  if (Array.isArray(spec) || (typeof spec === 'object' && spec !== null)) return spec
  if (typeof spec !== 'string') throw new Error(`region 无法识别：${String(spec)}`)
  const rows = await listWindows(side)
  if (spec === 'foreground') {
    const fg = rows.find((r) => r.foreground)
    if (!fg) throw new Error('当前没有前台窗口')
    return [fg.x, fg.y, fg.w, fg.h]
  }
  if (spec.startsWith('window:')) {
    const needle = spec.slice(7).toLowerCase()
    const hit = rows.find((r) => String(r.title).toLowerCase().includes(needle))
    if (!hit) throw new Error(`找不到标题包含「${spec.slice(7)}」的窗口。可用窗口：${rows.slice(0, 6).map((r) => r.title).join(' | ')}`)
    return [hit.x, hit.y, hit.w, hit.h]
  }
  // 允许字符串形式 "[x,y,w,h]" / "x,y,w,h"（工具的 schema 把数组转成字符串时走这条）
  if (/^\s*\[?\s*-?\d+\s*,\s*-?\d+\s*,\s*\d+\s*,\s*\d+\s*\]?\s*$/.test(spec)) {
    const nums = spec.match(/-?\d+/g).map(Number)
    if (nums.length === 4) return nums
  }
  throw new Error(`region 写法不对：${spec}（可用 full / foreground / window:关键词 / [x,y,w,h]）`)
}

function briefWindow(r) {
  return `${r.title}（${r.w}x${r.h} @${r.x},${r.y}${r.foreground ? '，前台' : ''}，hwnd ${r.hwnd}）`
}

// ---------------------------------------------------------------- 插件主体

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config ?? {}) }
  const side = new Sidecar(ctx.logger?.('coach-perceive') ?? ctx.logger, cfg)

  ctx.effect(() => {
    return () => side.stop()
  })

  ctx.inject(['tools'], (toolsCtx) => {
    const tools = toolsCtx.tools

    tools.register({
      name: 'coach_windows',
      description:
        '列出可见窗口（标题/位置/大小/是否前台），并报告感知能力是否可用。排盘面前先调它，拿到窗口再决定看哪块区域。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: TEXT_OUTPUT,
      async execute() {
        await side.ensure()
        const data = await side.call('/windows')
        const health = await side.call('/health')
        const rows = data.windows ?? []
        const lines = [
          `屏幕 ${health.screen?.join('x')}｜抓帧 ${health.ffmpeg === 'missing' ? '不可用' : '正常'}（${health.fps}fps，帧龄 ${health.frameAgeMs ?? '?'}ms）｜OCR 经 PowerShell 桥`,
          `可见窗口 ${rows.length} 个：`,
          ...rows.map((r, i) => `${i + 1}. ${briefWindow(r)}`),
        ]
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'coach_anchor',
      description:
        '把「看哪儿」锚定下来，之后所有感知工具默认只看这块区域，不用每回合猜坐标。action=obs 走 OBS(obs-websocket) 找采集源并解析出真实窗口矩形；action=window 按窗口标题/句柄锚定；action=rect 直接给矩形；action=get 看当前锚定；action=clear 取消。只做一次，窗口移动会自动跟随。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: { type: 'string', description: 'obs / window / rect / get / clear，默认 get' },
          source: { type: 'string', description: 'action=obs 时的 OBS 采集源名；省略则取第一个窗口采集源' },
          window: { type: 'string', description: 'action=obs 时顺便把该采集源切到这个窗口（标题片段），省得手动去 OBS 属性里换窗口' },
          title: { type: 'string', description: 'action=window 时的窗口标题或标题片段' },
          hwnd: { type: 'number', description: 'action=window 时也可直接给窗口句柄' },
          rect: { type: 'array', description: 'action=rect 时的 [x,y,w,h]', items: { type: 'number' } },
          port: { type: 'number', description: 'OBS WebSocket 端口，默认 4455（或插件配置 obsPort）' },
          password: { type: 'string', description: 'OBS WebSocket 密码（或配置 obsPassword / 环境变量 COACH_OBS_PASSWORD）' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const action = String(a.action ?? 'get').toLowerCase()
        if (action === 'get') {
          const data = await side.call('/anchor')
          if (!data.anchor) return { text: '当前没有锚定：感知默认看整屏。建议先 coach_anchor{action:"obs"} 或 {action:"window",title:"关键词"}。' }
          const an = data.anchor
          return {
            text: [
              `当前锚定：${an.kind}${an.source ? ` / 采集源 ${an.source}` : ''}${an.title ? ` / 窗口 ${an.title}` : ''}`,
              `区域 [${(data.region ?? an.rect ?? []).join(',')}]`,
              an.scene ? `OBS 当前场景：${an.scene}` : '',
            ].filter(Boolean).join('\n'),
          }
        }
        if (action === 'clear') {
          const data = await side.call('/anchor', { kind: 'clear' })
          return { text: data.anchor ? '清除失败' : '已取消锚定，感知回到整屏。' }
        }
        if (action === 'obs') {
          await side.ensure()
          const body = { kind: 'obs', source: a.source, port: a.port, password: a.password, window: a.window }
          const data = await side.call('/anchor', body, 40000)
          const an = data.anchor
          return {
            text: [
              `已锚定 OBS 采集源「${an.source}」（${an.sourceKind}，场景 ${an.scene}）${an.retargeted ? '，并已把采集源切到目标窗口' : ''}`,
              `→ 窗口 ${an.title}（hwnd ${an.hwnd}），区域 [${(an.rect ?? []).join(',')}]`,
              '之后 coach_diff / coach_snapshot / coach_frame 不传 region 就只看这块。',
            ].join('\n'),
          }
        }
        if (action === 'window') {
          const data = await side.call('/anchor', { kind: 'window', title: a.title, hwnd: a.hwnd }, 20000)
          const an = data.anchor
          return { text: `已锚定窗口「${an.title}」，区域 [${an.rect.join(',')}]。之后不传 region 就只看这块。` }
        }
        if (action === 'rect') {
          const data = await side.call('/anchor', { kind: 'rect', rect: a.rect })
          return { text: `已锚定矩形 [${data.region.join(',')}]。` }
        }
        throw new Error(`action 只能是 obs / window / rect / get / clear，收到 ${action}`)
      },
    })

    tools.register({
      name: 'coach_diff',
      description:
        '判断盘面相对上次调用是否变化，只回变化的格子坐标（默认整屏 32px 网格），不传像素、不花视觉 token。回合游戏每步先调它：没变就别再看。settleMs>0 时改为「等到画面静止」再返回。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '不传=锚定区域（没锚定则整屏）/ full / foreground / window:标题片段 / [x,y,w,h]' },
          settleMs: { type: 'number', description: '等待画面静止的最长毫秒数，默认 0 表示立即比较' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const region = await resolveRegion(side, a.region)
        if (Number(a.settleMs) > 0) {
          const data = await side.call('/watch', { region, ms: Number(a.settleMs) }, 20000)
          return { text: data.stable ? `画面已静止（等待 ${data.waitedMs}ms）。` : `画面在 ${data.waitedMs}ms 内仍在变化，可能还在动画/加载。` }
        }
        const data = await side.call('/diff', { region })
        if (data.first) return { text: `首次比较（区域 ${data.region?.join(',')}），已建立基线；再调一次才能判断变化。` }
        if (!data.count) return { text: `无变化（区域 ${data.region?.join(',')}，帧 ${data.frameSeq}，帧龄 ${data.frameAgeMs}ms）。不需要重新看画面。` }
        const boxes = data.changed ?? []
        if (data.boxes) {
          return {
            text: [
              `有变化：${data.count} 个格子，合并成 ${data.boxes} 块（屏幕坐标，直接照着点）｜区域 ${data.region?.join(',')}｜抓帧 ${data.source ?? '?'}`,
              ...boxes.map((b) => `  块 [${b[0]},${b[1]} ${b[2]}x${b[3]}]`),
            ].join('\n'),
          }
        }
        return {
          text: [
            `有变化：${data.count} 个格子（32px 网格，相对区域左上角），帧 ${data.frameSeq}。`,
            '变化格子：' + boxes.map((b) => `(${b[0]},${b[1]})`).join(' '),
          ].join('\n'),
        }
      },
    })

    tools.register({
      name: 'coach_snapshot',
      description:
        '取当前画面（可指定区域）。ocr=true 时返回本地 OCR 的文字行（含坐标），零视觉 token——读资源数字/回合数/单位名就用它。返回里 changedTiles 告诉你相对上次调用变了多少格。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '不传=锚定区域（没锚定则整屏）/ full / foreground / window:标题片段 / [x,y,w,h]' },
          ocr: { type: 'boolean', description: '是否附带本地 OCR 结果，默认 false' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const region = await resolveRegion(side, a.region)
        const data = await side.call('/snapshot', { region, ocr: a.ocr === true }, a.ocr ? 90000 : undefined)
        const s = data.snapshot
        const lines = [
          `画面 ${s.size[0]}x${s.size[1]}（区域 ${s.region?.join(',')}，网格 ${s.tiles.join('x')}，sha ${s.sha1}，帧龄 ${s.frameAgeMs}ms，抓帧 ${s.source ?? '?'}${s.anchor?.title ? `｜窗口 ${s.anchor.title}` : ''}）`,
          s.changedTiles === null || s.changedTiles === undefined ? '变化格：基线' : `变化格：${s.changedTiles}/${s.tiles[0] * s.tiles[1]}`,
        ]
        if (s.ocr) {
          const weakTag = s.ocrWeak ? '（短文本抢救，置信度低，仅作线索）' : ''
          lines.push(`OCR（${s.ocrLang}，耗时 ${s.ocrMs}ms，${s.ocr.length} 行${weakTag}；坐标=屏幕绝对坐标）：`)
          for (const ln of s.ocr.slice(0, 120)) lines.push(`  [${ln.x},${ln.y} ${ln.w}x${ln.h}] ${ln.text}`)
        }
        if (s.coveredBy?.title) {
          lines.push(`⚠ 锚定窗口此刻被「${s.coveredBy.title}」盖住（点 ${s.coveredBy.at?.join(',')}）——读画面不受影响，但你按鼠标会点到上层窗口。`)
        }
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'coach_facts',
      description:
        '把当前画面转成「规则引擎能吃的 facts JSON」并存到 %USERPROFILE%\\.dsh\\coach-perceive\\last-facts.json。之后直接 turn_card / rules_run 用 useCoachFacts:true 读它，不用手抄 JSON。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '不传=锚定区域；full / foreground / window:标题 / [x,y,w,h]' },
          save: { type: 'boolean', description: '是否落盘给工作台读，默认 true' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const region = await resolveRegion(side, a.region)
        const data = await side.call('/facts', { region }, 90000)
        const facts = data && typeof data === 'object' ? { ...data } : {}
        delete facts.ok
        const lines = [
          `facts 已生成：场景 ${facts.scene ?? '?'}｜实体 ${(facts.entities ?? []).length} 个｜抓帧 ${facts.source ?? '?'}`,
          `数值：${Object.entries(facts.values ?? {}).map(([k, v]) => `${k}=${v}`).join('，') || '（无）'}`,
        ]
        for (const e of (facts.entities ?? []).slice(0, 25)) lines.push(`  ${e.id} [${e.kind}] ${e.text}${e.value === null || e.value === undefined ? '' : `（值 ${e.value}）`} @${e.x},${e.y}`)
        if (a.save !== false) {
          const dir = join(homedir(), '.dsh', 'coach-perceive')
          mkdirSync(dir, { recursive: true })
          const file = join(dir, 'last-facts.json')
          writeFileSync(file, JSON.stringify({ ...facts, __savedAt: new Date().toISOString() }, null, 2), 'utf8')
          lines.push(`已落盘：${file}（工作台用 useCoachFacts:true 直接读）`)
        }
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'coach_frame',
      description:
        '把区域存成 PNG；attach=true 时把图直接作为图像块交给模型看（这一步才真的花视觉 token）。只在 OCR 不可靠、需要看图形/地图/图标时使用。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '不传=锚定区域（没锚定则整屏）/ full / foreground / window:标题片段 / [x,y,w,h]' },
          attach: { type: 'boolean', description: '是否把图片作为图像块返回，默认 false（只给路径）' },
        },
      },
      output: FRAME_OUTPUT,
      async execute(args, exec) {
        const a = args && typeof args === 'object' ? args : {}
        const region = await resolveRegion(side, a.region)
        const data = await side.call('/frame', { region })
        const desc = `已保存画面 ${data.size?.[0]}x${data.size?.[1]}（区域 ${data.region?.join(',')}）→ ${data.path}`
        if (a.attach !== true) return { text: desc }
        const attachments = ctx.get?.('attachments')
        if (!attachments?.saveImage) return { text: `${desc}\n（当前没有 attachments 服务，无法内联图片；请用 read_image 读取该路径）` }
        try {
          const bytes = readFileSync(data.path)
          const ref = await attachments.saveImage({ data: new Uint8Array(bytes), mediaType: 'image/png', name: 'coach-frame.png' })
          return { text: desc, image: ref }
        } catch (err) {
          return { text: `${desc}\n内联图片失败：${err?.message ?? err}` }
        }
      },
    })

    tools.register({
      name: 'coach_key',
      description:
        '向锚定窗口按一个键。危险键（space/enter）在战斗/结算/舞台编成界面会被护栏拒绝——那里的 space 等于「自动结算」，会替你把整场打完。真要在这些界面按，必须显式给 force:true。回执里有真实前台窗口和「按完画面变了几格」。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          key: { type: 'string', description: '按键名，如 space / enter / esc / up / down / left / right / 1 / a' },
          hwnd: { type: 'number', description: '目标窗口句柄，不传用当前锚定窗口' },
          mode: { type: 'string', enum: ['input', 'post'], description: 'input=SendInput（默认，最真）；post=PostMessage（切不了前台时用）' },
          dryRun: { type: 'boolean', description: '只判断会不会被拦，不真按' },
          force: { type: 'boolean', description: '绕过危险键护栏（会记进 input-log）' },
          verify: { type: 'boolean', description: '按完看变化，默认 true' },
          verifyMs: { type: 'number', description: '按完等多久再看，默认 450ms' },
          holdMs: { type: 'number', description: '按住多少毫秒，默认 30' },
        },
        required: ['key'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        if (cfg.inputEnabled !== true) {
          return { text: '输入未启用：把插件配置 inputEnabled 改成 true 再试（位置：设置 → 插件 → 游戏感知）。' }
        }
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/input/key', {
          key: a.key, hwnd: a.hwnd, mode: a.mode, dryRun: a.dryRun === true,
          force: a.force === true, verify: a.verify !== false,
          verifyMs: a.verifyMs, holdMs: a.holdMs,
        }, 20000)
        if (data.refused) {
          return { text: `⛔ 已阻止，一个键都没按：${data.reason}\n　${data.howTo}` }
        }
        if (data.dryRun) {
          return { text: `（演练）会按「${data.key}」，当前场景 ${data.scene}，真实前台 ${data.foreground?.title || '?'}（${data.foreground?.hwnd ?? '?'}）。没有被拦。` }
        }
        const fg = data.foreground_ok === false
          ? `\n⚠ 真实前台不是目标窗口：${data.actual_foreground_title || '无标题'}（${data.actual_foreground_hwnd}）——这一下很可能落到了别的窗口`
          : ''
        const chg = data.changedTiles == null ? '' : `，画面变化 ${data.changedTiles} 格${data.changedTiles === 0 ? '（按键可能无效）' : ''}`
        return { text: `已按「${data.key}」→ hwnd ${data.hwnd}（场景 ${data.sceneZh || data.scene}${chg}，SendInput 返回 ${data.sent}）${fg}${data.hint ? `\n${data.hint}` : ''}` }
      },
    })

    tools.register({
      name: 'coach_click',
      description:
        '点一个东西。优先用 element:"ui_3"（来自 coach_ui_dump，最稳，不用猜坐标）；也可以 x/y 直接给屏幕坐标，或 text:"发送邀请" 按文字找。点击前会**真的抢前台**并复核，抢不到就报错而不是假装成功；点完回执带「变化了几格」和真实前台窗口名。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          element: { type: 'string', description: '元素 id，如 ui_3（先 coach_ui_dump）' },
          x: { type: 'number', description: '屏幕 x（和 y 一起用）' },
          y: { type: 'number', description: '屏幕 y' },
          text: { type: 'string', description: '按文字点（子串匹配 OCR 行）' },
          hwnd: { type: 'number', description: '目标窗口句柄，不传用当前锚定窗口' },
          moveSettleMs: { type: 'number', description: '指针移到目标后等多久再按下（让 hover 生效），默认 250ms' },
          downUpMs: { type: 'number', description: '按下到抬起的间隔，默认 80ms' },
          verifyMs: { type: 'number', description: '点完等多久再比画面，默认 450ms' },
          dryRun: { type: 'boolean', description: '只算坐标不真点' },
          force: { type: 'boolean', description: '跳过场景提示（不影响危险键护栏）' },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        if (cfg.inputEnabled !== true) {
          return { text: '输入未启用：把插件配置 inputEnabled 改成 true 再试（位置：设置 → 插件 → 游戏感知）。' }
        }
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/click', {
          element: a.element, x: a.x, y: a.y, text: a.text, hwnd: a.hwnd,
          moveSettleMs: a.moveSettleMs, downUpMs: a.downUpMs, verifyMs: a.verifyMs,
          dryRun: a.dryRun === true, force: a.force === true,
        }, 30000)
        if (data.dryRun) {
          return { text: `（演练）会点 ${data.element} @ (${data.wouldClick.join(',')})${data.note ? `\n${data.note}` : ''}` }
        }
        const chg = data.changedTiles == null ? '?' : data.changedTiles
        const fg = data.foreground_ok === false
          ? `\n⚠ 真实前台是「${data.actual_foreground_title || '无标题'}」（${data.actual_foreground_hwnd}），不是目标窗口`
          : `\n真实前台：${data.actual_foreground_title || '无标题'}（${data.actual_foreground_hwnd}）`
        const boxes = arr(data.changedRegion).slice(0, 3).map((b) => `[${b.join(',')}]`).join(' ')
        return {
          text: `✓ 点了 ${data.element} @ (${data.clicked_screen_xy.join(',')}) · 变化 ${chg} 格${boxes ? ` ${boxes}` : ''}${fg}${data.hint ? `\n${data.hint}` : ''}${data.note ? `\n${data.note}` : ''}`,
        }
      },
    })

    tools.register({
      name: 'coach_ui_dump',
      description:
        '把画面上能点的东西列成表：每个元素有 id（ui_1、ui_2…）、类型（button/close/slot/card/panel/text/stat）、文字 label、屏幕框 box、是否置灰 enabled。之后用 coach_click{element:"ui_3"} 点它，不用猜坐标。描边字 OCR 读不出时会标 src=blob（靠亮度+平整度找到的块）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '只 dump 这块：[x,y,w,h] / "anchor"（默认）/ "foreground"' },
          limit: { type: 'number', description: '最多返回几个元素，默认 40' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/ui_dump', { region: a.region, limit: a.limit ?? 40 }, cfg.callTimeoutMs)
        const rows = arr(data.elements).map((e) => {
          const lab = e.label ? `「${e.label}」` : ''
          const off = e.enabled === false ? ' ⛔置灰' : ''
          return `  ${e.id}  ${e.type}${lab}  [${e.box.join(',')}]${off}${e.src === 'blob' ? ' (图形块)' : ''}`
        })
        const head = `场景=${data.sceneZh || data.scene}｜元素 ${data.counts?.all ?? 0} 个（按钮 ${data.counts?.button ?? 0}，槽位/卡 ${data.counts?.slot ?? 0}）｜OCR ￥${data.ocrMs ?? '?'}ms`
        return { text: [head, ...rows].join('\n') }
      },
    })

    tools.register({
      name: 'coach_wait_stable',
      description:
        '等画面静止（用于「动画/淡入结束再点」）。用单格色差判断，背景粒子/水波不会再让它永远"在动"。返回 stable/stillMoving 与等待时长。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '看哪块，默认锚定区域' },
          settleMs: { type: 'number', description: '连续静止多久算稳定，默认 400ms' },
          timeoutMs: { type: 'number', description: '最多等多久，默认 5000ms' },
          minTiles: { type: 'number', description: '允许几格在动（默认自动：约 1.2% 的格子）' },
          regionIgnore: {
            type: 'array',
            items: { type: 'array', items: { type: 'number' } },
            description: '这些矩形（屏幕坐标）里的变化不算',
          },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/wait_stable', {
          region: a.region, settleMs: a.settleMs ?? 400, timeoutMs: a.timeoutMs ?? 5000,
          minTiles: a.minTiles, ignore: a.regionIgnore,
        }, 30000)
        return {
          text: data.stable
            ? `画面已静止：等了 ${data.waitedMs}ms（容差 ${data.tol} 格，门槛 ${data.thr}）`
            : `画面还在动：等了 ${data.waitedMs}ms，最多有 ${data.movingTiles} 格在变（容差 ${data.tol} 格）。多半还在动画/加载。`,
        }
      },
    })

    tools.register({
      name: 'coach_guard',
      description:
        '查危险动作护栏：不给 key 就返回整张「场景 × 按键」表；给 key 就判断这个键在当前场景会不会被拦（refuse/allow）。战斗界面 space/enter 一律拒绝——那是自动结算。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          key: { type: 'string', description: '要问的键，如 space' },
          scene: { type: 'string', description: '指定场景（不传就现抓一帧判断）' },
          force: { type: 'boolean', description: '按"已经打算 force 绕过"来算' },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/guard', { key: a.key, scene: a.scene, force: a.force === true }, 20000)
        if (data.key) {
          const v = data.verdict === 'refuse' ? '⛔ 会被拒绝' : '✓ 放行'
          return { text: `${v}：场景 ${data.sceneZh || data.scene} 里按「${data.key}」${data.reason ? `\n理由：${data.reason}` : ''}${data.howTo ? `\n${data.howTo}` : ''}` }
        }
        const rows = arr(data.table).map((r) => `  ${r.sceneZh.padEnd(6, '　')} ${r.key.padEnd(6)} ${r.verdict === 'refuse' ? '⛔ 拒绝' : '✓ 允许'}  ${r.note}`)
        return { text: [`当前场景：${data.sceneZh || data.scene}`, ...rows].join('\n') }
      },
    })

    tools.register({
      name: 'coach_click_until',
      description:
        '点一次没反应就再点（动画结束后才生效的按钮、需要点两次的按钮用它）。每点一次都回报变化格数，变了就停。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          element: { type: 'string', description: '元素 id（先 coach_ui_dump）' },
          x: { type: 'number', description: '屏幕 x' },
          y: { type: 'number', description: '屏幕 y' },
          text: { type: 'string', description: '按文字点' },
          maxTries: { type: 'number', description: '最多点几次，默认 3' },
          intervalMs: { type: 'number', description: '两次之间等多久，默认 700ms' },
          expectChange: { type: 'number', description: '变化几格算成功，默认 1' },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        if (cfg.inputEnabled !== true) {
          return { text: '输入未启用：把插件配置 inputEnabled 改成 true 再试。' }
        }
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/click_until', {
          element: a.element, x: a.x, y: a.y, text: a.text,
          maxTries: a.maxTries ?? 3, intervalMs: a.intervalMs ?? 700, expectChange: a.expectChange ?? 1,
        }, 60000)
        const rows = arr(data.attempts).map((t) => `  第 ${t.n} 次：变化 ${t.changed ?? '?'} 格`)
        return {
          text: data.ok
            ? `✓ 第 ${data.tries} 次生效（${data.stoppedBy}）\n${rows.join('\n')}`
            : `✗ ${data.tries} 次都没反应\n${rows.join('\n')}\n${data.hint || ''}`,
        }
      },
    })

    tools.register({
      name: 'coach_dismiss',
      description:
        '通用关闭：先找画面上的 ×/关闭 点它，再按 Esc，再点空白——每一步都实测画面有没有变，变了就报是哪一招成功的。教学浮层、暂停菜单、弹窗都用它。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          prefer: { type: 'string', enum: ['ui', 'esc', 'click'], description: '先试哪一招，默认按 ×→Esc→空白' },
          waitMs: { type: 'number', description: '每招之后等多久，默认 500ms' },
          hwnd: { type: 'number', description: '目标窗口句柄' },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        if (cfg.inputEnabled !== true) {
          return { text: '输入未启用：把插件配置 inputEnabled 改成 true 再试。' }
        }
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/dismiss', { prefer: a.prefer, waitMs: a.waitMs, hwnd: a.hwnd }, 45000)
        const rows = arr(data.tries).map((t) => `  ${t.strategy}: ${t.done ? (t.what || '') : (t.why || '跳过')}${t.changedTiles != null ? ` → 变化 ${t.changedTiles} 格` : ''}`)
        return {
          text: data.ok
            ? `✓ 关掉了（用「${data.closedBy}」）：${data.what}\n${rows.join('\n')}`
            : `✗ 没关掉：${data.error || ''}\n${rows.join('\n')}`,
        }
      },
    })

    tools.register({
      name: 'coach_key_probe',
      description:
        '逐个按键实测「这个键在这里到底有没有用」：按一下、比画面、记变化格数。游戏里写着能按的键不一定真响应（废墟图书馆底部写 Q 选择，实际无效）。危险键会被护栏跳过。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          keys: { type: 'array', items: { type: 'string' }, description: '要试的键，默认 space/enter/esc/q/e/1/2' },
          waitMs: { type: 'number', description: '每按一个等多久，默认 400ms' },
          force: { type: 'boolean', description: '危险键也试（会记日志）' },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        if (cfg.inputEnabled !== true) {
          return { text: '输入未启用：把插件配置 inputEnabled 改成 true 再试。' }
        }
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/key_probe', { keys: a.keys, waitMs: a.waitMs, force: a.force === true }, 90000)
        const rows = arr(data.keys).map((r) => {
          if (r.blocked) return `  ${r.key.padEnd(6)} ⛔ 护栏拦下：${r.reason}`
          if (r.tested === false) return `  ${r.key.padEnd(6)} ✗ 按不出去：${r.error}`
          return `  ${r.key.padEnd(6)} ${r.works ? '✓ 有反应' : '· 无反应'}（${r.changedTiles} 格）`
        })
        return { text: [`场景：${data.sceneZh || data.scene}`, ...rows].join('\n') }
      },
    })

    tools.register({
      name: 'coach_click_log',
      description: '看最近的动手记录：每次点击的时间、目标窗口、真实前台、坐标、命中元素、变化格数。排查「点了没反应」用它。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { limit: { type: 'number', description: '看最近几条，默认 20' } },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/click_log', { limit: a.limit ?? 20 }, 20000)
        const rows = arr(data.rows).map((r) => {
          const when = new Date((r.at ?? 0) * 1000).toLocaleTimeString()
          const tgt = r.x != null ? `(${r.x},${r.y})` : (r.key || '?')
          const fg = r.foregroundTitle ? `｜前台「${r.foregroundTitle}」` : ''
          const extra = r.blocked ? '｜被护栏拦下' : (r.changed != null ? `｜变化 ${r.changed} 格` : '')
          return `  ${when} ${tgt}${r.via ? ` via ${r.via}` : ''}${fg}${extra}`
        })
        return { text: rows.length ? [`日志：${data.path}`, ...rows].join('\n') : (data.note || '没有记录') }
      },
    })

    tools.register({
      name: 'coach_scene_define',
      description:
        '把当前画面注册成一个场景模板（截图特征 + 关键词）。描边字 OCR 读不出时，靠模板也能认场景；认出来之后护栏和 stopScenes 才生效。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          name: { type: 'string', description: '场景名，如 battle / invite / 邀请函' },
          kind: { type: 'string', description: '归类，如 battle/menu/dialog（可选）' },
          keywords: { type: 'array', items: { type: 'string' }, description: '识别关键词（不传就自动取画面文字）' },
          note: { type: 'string', description: '备注' },
        },
        required: ['name'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/scene/define', {
          name: a.name, kind: a.kind, keywords: a.keywords, note: a.note,
        }, cfg.callTimeoutMs)
        return { text: `已注册场景「${data.saved}」（现在共 ${data.total} 个）。关键词：${(data.keywords ?? []).join('、') || '（无）'}` }
      },
    })

    tools.register({
      name: 'coach_scene_list',
      description: '列出已经注册的场景模板。',
      parameters: { type: 'object', additionalProperties: false, properties: {}, required: [] },
      output: TEXT_OUTPUT,
      async execute() {
        const data = await side.call('/scene/list', {}, 20000)
        if (!data.count) return { text: '还没有注册任何场景模板。用 coach_scene_define{name:"battle"} 把当前画面注册下来。' }
        const rows = arr(data.scenes).map((s) => `  ${s.name}（${s.kind || 'custom'}）关键词：${(s.keywords ?? []).join('、') || '无'}`)
        return { text: [`共 ${data.count} 个场景模板：`, ...rows].join('\n') }
      },
    })

    tools.register({
      name: 'coach_now',
      description:
        '一次调用看清当前局面（最省 token 的看盘口）：返回场景（dialog/choice/battle/menu）、相对上次的变化格数和变化区域、以及画面下半部分的 OCR 文字行（带屏幕绝对坐标）。没变化时只有几十 token。判断「现在能不能点下一步」通常先用它。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          maxLines: { type: 'number', description: '最多返回几行文字，默认 10' },
          bandFrom: { type: 'number', description: '从窗口高度的百分比开始 OCR（0.5=只读下半屏，默认 0.5；给 0 表示整屏）' },
          ocr: { type: 'boolean', description: '是否顺手 OCR，默认 true' },
          boxes: { type: 'boolean', description: '是否把文字行聚成块，方便一次处理整块区域' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/now', { maxLines: a.maxLines ?? 10, bandFrom: a.bandFrom ?? 0.5, ocr: a.ocr !== false, boxes: a.boxes === true }, cfg.callTimeoutMs)
        const ch = data.changed ?? {}
        const conf = data.confidence != null ? `（置信 ${data.confidence}${data.sceneSource ? `/${data.sceneSource}` : ''}）` : ''
        const ev = (data.evidence ?? []).length ? `｜证据：${data.evidence.join('、')}` : ''
        const anim = data.animating ? '｜⚠画面还在动' : ''
        const head = `场景=${data.sceneZh || data.scene || '?'}${conf}｜变化格=${ch.count ?? 0}｜变化区域=${ch.total ?? 0}${ev}${anim}`
        const boxes = (ch.boxes ?? []).map((b) => `[${b.join(',')}]`).join(' ')
        const lines = arr(data.lines).map((l) => `[${l.x},${l.y}] ${l.text}`).join('\n')
        const tips = data.textReadable === false ? '（这套字体 OCR 读不出字，场景是靠画面结构认的）' : ''
        return {
          text: [head, tips, boxes ? `变化区域：${boxes}` : '', lines].filter(Boolean).join('\n'),
        }
      },
    })

    tools.register({
      name: 'coach_drive',
      description:
        '一步顶一串：对着锚定窗口连续按同一个键（默认空格）推进对话/翻页。**按第一次之前先认场景**：战斗/结算/舞台编成界面按 space 等于自动结算，会被护栏直接拒绝（要按必须 force:true）。每步都回报场景、证据和停在第几步、为什么停。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          key: { type: 'string', description: '按键名，默认 space' },
          maxSteps: { type: 'number', description: '最多按几次，默认 20（上限 120）' },
          intervalMs: { type: 'number', description: '每次按键间隔毫秒，默认 400' },
          stopScenes: {
            type: 'array',
            items: { type: 'string' },
            description: '遇到这些场景就停，默认 ["choice","battle","battle_setup","result"]',
          },
          requireScene: { type: 'string', description: '只在这个场景里推进（如 dialog），不满足就一个键都不按' },
          force: { type: 'boolean', description: '绕过危险键护栏（会记日志）' },
          dryRun: { type: 'boolean', description: '只认场景、报计划，不按键' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        if (cfg.inputEnabled !== true) {
          return { text: '输入未启用：把插件配置 inputEnabled 改成 true 再试（位置：设置 → 插件 → 游戏感知）。' }
        }
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call(
          '/drive',
          {
            key: a.key ?? 'space', maxSteps: a.maxSteps ?? 20, intervalMs: a.intervalMs ?? 400,
            stopScenes: a.stopScenes, requireScene: a.requireScene,
            force: a.force === true, dryRun: a.dryRun === true,
          },
          Math.max(cfg.callTimeoutMs, 60000),
        )
        if (data.refused) {
          return { text: `⛔ 一步都没按：${data.reason || '被护栏拒绝'}\n　当前场景：${data.sceneZh || data.scene}${data.howTo ? `\n　${data.howTo}` : ''}` }
        }
        if (data.dryRun) {
          return { text: `（演练）当前场景 ${data.sceneZh || data.scene}，${data.plan}。没有被拦。` }
        }
        const steps = arr(data.steps).map((s) => `  第 ${s.n} 步：${s.pressed ? '按了' : '没按'} ${s.scene}${s.why ? `（${s.why}）` : ''}`)
        const ev = (data.evidence ?? []).length ? `｜证据：${data.evidence.join('、')}` : ''
        const head = `按了 ${data.pressed} 次，停在 ${data.stoppedBy}，场景 ${data.sceneZh || data.scene}${ev}`
        const why = data.why ? `\n${data.why}` : ''
        const lines = arr(data.lines).slice(0, 6).map((l) => `  [${l.x},${l.y}] ${l.text}`).join('\n')
        return { text: [head, why, ...steps, lines].filter(Boolean).join('\n') }
      },
    })

    tools.register({
      name: 'coach_state_json',
      description:
        '战斗盘面状态源（《废墟图书馆》优先）：一次返回 scene/sub、自己与敌人头顶的速度骰（含屏幕坐标）、底部手牌（含屏幕坐标）、HP/混乱条、「下令战斗」按钮坐标、是否静止、以及读不出来的字段清单。坐标全是屏幕绝对坐标，可以直接喂给 coach_click。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          hp: { type: 'boolean', description: '是否顺便量底部血条（默认 true，慢一点）' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/state', { hp: a.hp !== false }, cfg.callTimeoutMs)
        return { text: renderState(d) }
      }
    })

    tools.register({
      name: 'coach_select_page',
      description:
        '出牌第 1-2 步：先点自己头顶的速度骰，再点底部手牌（卡面会放大显示骰值）。回执逐步给"点在哪、变化几格"，手牌没开时直接报原因。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          die: { type: 'number', description: '点第几个自己的速度骰（默认 0）' },
          hand: { type: 'number', description: '点第几张手牌（默认 0）' },
          dryRun: { type: 'boolean', description: '只算坐标不真点' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/select_page', { die: a.die ?? 0, hand: a.hand ?? 0, dryRun: !!a.dryRun }, cfg.callTimeoutMs)
        const steps = arr(d.steps).map((s) => `  ${s.step}：「${s.element ?? '?'}」@(${s.at?.[0]},${s.at?.[1]}) 变化 ${s.changedTiles ?? '?'} 格${s.ok === false ? ' ✗' : ''}`).join('\n')
        return { text: `${d.dryRun ? '（演练）' : '已出牌'}${d.dryRun ? '' : '两步'}，手牌 ${d.handCount ?? '?'} 张\n${steps}${d.hint ? '\n' + d.hint : ''}` }
      },
    })

    tools.register({
      name: 'coach_target_enemy',
      description:
        '出牌第 3 步：点敌方头顶的速度骰指敌（画面上会多一条弧线连两个骰）。回执里的 verdict 会说"亮一下=指派上了 / 没变=没点上"。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          die: { type: 'number', description: '点第几个敌方速度骰（默认 0）' },
          dryRun: { type: 'boolean', description: '只算坐标不真点' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/target_enemy', { die: a.die ?? 0, dryRun: !!a.dryRun }, cfg.callTimeoutMs)
        if (d.ok === false) return { text: `✗ 没指上：${d.error}` }
        return { text: `点了敌方骰 ${a.die ?? 0} @(${d.clicked?.[0]},${d.clicked?.[1]})，变化 ${d.changedTiles ?? '?'} 格\n${d.verdict ?? ''}` }
      },
    })

    tools.register({
      name: 'coach_cancel',
      description: '取消当前选择（右键）。游戏里没写能不能取消时先用它——比乱点安全。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { x: { type: 'number' }, y: { type: 'number' } },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/cancel_selection', a.x != null ? { x: a.x, y: a.y } : {}, cfg.callTimeoutMs)
        return { text: d.ok === false ? `✗ 取消失败：${d.error}` : `右键取消 @(${d.clicked?.[0]},${d.clicked?.[1]})，变化 ${d.changedTiles ?? '?'} 格` }
      },
    })

    tools.register({
      name: 'coach_execute',
      description:
        '出牌最后一步：点顶部「下令战斗」，用"画面大面积变化"判成功（成功 400+ 格；只弹面板约 135 格；1.2s 内可能只测到 93 格，所以默认等 2.2s）。失败时说清是没指派还是不在战斗界面。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          expectMin: { type: 'number', description: '变化多少格算成功，默认 250' },
          verifyMs: { type: 'number', description: '点完等多久再比，默认 2200' },
          dryRun: { type: 'boolean', description: '只算坐标不真点' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/execute', { expectMin: a.expectMin ?? 250, verifyMs: a.verifyMs ?? 2200, dryRun: !!a.dryRun }, cfg.callTimeoutMs)
        if (d.ok === false) {
          const tried = arr(d.tried).map((t) => `  第${t.attempt}次 @(${t.at?.[0]},${t.at?.[1]}) 变化 ${t.changedTiles ?? '?'} 格`).join('\n')
          return { text: `✗ 下令没成：${d.error}${tried ? '\n试过：\n' + tried : ''}` }
        }
        return { text: `✓ 下令成功 @(${d.clicked?.[0]},${d.clicked?.[1]})（第 ${d.attempt} 次尝试，变化 ${(d.tried ?? [])[d.attempt - 1]?.changedTiles ?? '?'} 格）\n接着 coach_now / coach_state_json 看结算。` }
      },
    })

    tools.register({
      name: 'coach_click_text',
      description:
        '按文字点：在当前画面 OCR 结果里找含指定文字的那一行，点它的中心（菜单项、选项、按钮都适用），坐标自动算，不用你猜。匹配多行时用 index 选第几个。需要 inputEnabled=true。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          text: { type: 'string', description: '要点的文字（子串即可）' },
          index: { type: 'number', description: '多行匹配时点第几个，默认 0' },
          dy: { type: 'number', description: '在行中心基础上纵向微调像素，默认 0' },
        },
        required: ['text'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        if (cfg.inputEnabled !== true) {
          return { text: '输入未启用：把插件配置 inputEnabled 改成 true 再试（位置：设置 → 插件 → 游戏感知）。' }
        }
        const a = args && typeof args === 'object' ? args : {}
        const data = await side.call('/click_text', { text: a.text, index: a.index ?? 0, dy: a.dy ?? 0 }, cfg.callTimeoutMs)
        const weakNote = data.weak ? '\n⚠ 这是短文本抢救（放大+拼版）认出来的，置信度低：点完用 coach_snapshot 复核一眼。' : ''
        return { text: `点了「${data.clicked.text}」→ (${data.clicked.x},${data.clicked.y})，候选 ${data.matches} 个${weakNote}` }
      },
    })
  })

  ctx.inject(['systemPrompt'], (promptCtx) => {
    promptCtx.systemPrompt.section({
      name: 'coach-perceive-v3',
      order: 905,
      text: [
        '## 看盘面的省钱顺序（回合制/可暂停游戏）',
        '1. `coach_windows` 看有哪些窗口；`coach_anchor{action:"obs"}`（装了 OBS 且开了 WebSocket 时最准，能直接认出游戏采集源）或 `coach_anchor{action:"window",title:"关键词"}` 锚定一次，之后不必再猜坐标。',
        '2. 每步先 `coach_now`：它一次给「场景 + 变化格 + 下半屏文字」；没变化就别再看画面。',
        '3. 要更细的文字用 `coach_snapshot{ocr:true}`（本地 OCR，零视觉 token）。',
        '4. 只有 OCR 读不出（图标、地图、单位位置）才 `coach_frame{attach:true}` 真看图，一次一张、只框相关区域。',
        '5. 判不动时等画面静止（`coach_diff{settleMs}`）再读，避免抓到动画中间帧。',
        '## 能动手的时候（需要 inputEnabled）',
        '- 推进对话/翻页：`coach_drive{key:"space", maxSteps:20}` 一次走十几步，遇到选项/战斗会自己停。',
        '- 点菜单/选项：`coach_click_text{text:"确定"}`，坐标由 OCR 自动算，别手填。',
        '- 单步：`coach_key` / `coach_click`（坐标用 coach_now 给的屏幕绝对坐标）。',
        '- 每次操作会短暂夺前台，结束后自动还给原来的窗口；窗口被遮挡也不影响读屏（PrintWindow）。',
        '锚定区域会随窗口移动自动更新；要看整屏时显式传 region:"full"。',
      ].join('\n'),
    })
  })
}
export const __render = { renderState, arr, obj, txt, px }
