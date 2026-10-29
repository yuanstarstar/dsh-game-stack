/**
 * @local/dsh-cv-recall —— 感知编译器（Percept Compiler）
 *
 * 解决的问题：让模型"看清画面"目前等于"截一张图问一次模型"，每次 1.5~3k token、1~10 秒。
 * 但游戏界面是**高度重复**的：血条、小地图、技能图标、对话框，位置固定、外观固定，
 * 真正变的只有数字和内容。既然如此，把"识别"这件事编译成一段本地代码，只付一次钱：
 *
 *   1. 第一次（cv_learn）：模型看一眼画面，只输出「框在哪 + 叫什么」——不描述外观，
 *      外观由本地代码从框内像素自己算（颜色直方图 / 梯度 / 结构哈希 / 分块相关）；
 *   2. 编译成 `.cvpack.json`（几百 KB 的资产，含模板与阈值）；
 *   3. 之后每帧（cv_recall）只跑 numpy：0 token、几十毫秒、结构化 JSON。
 *
 * 两个工具：
 *   - cv_learn ：scan（免费给候选框）/ build（编译）/ add_scene（加场景并顺手校准阈值）
 *                / list / get / delete
 *   - cv_recall：一帧 → {scene, entities:[{id,present,bbox,conf}], changed, stale, ms}
 *
 * 设计上刻意让工具**自检**：画面变了（换主题/改分辨率/内容大改）时它自己报 stale，
 * 而不是继续给出自信的错误答案 —— 这样调用方永远不需要"怀疑"它。
 */

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'cv-recall'

const HERE = dirname(fileURLToPath(import.meta.url))
const SIDECAR = join(HERE, 'sidecar', 'cvcd.py')

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string' } },
    required: ['text'],
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

/** cv_learn{action:"scan"} 可以顺手把候选框画在图上给模型看：有图就渲染成图像块。 */
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

const DEFAULTS = { callTimeoutMs: 60000, root: '' }

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
  const explicit = config?.python || process.env.DSH_CV_PYTHON
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
    const args = [SIDECAR, '--port', '0']
    const root = this.config?.root || DEFAULTS.root
    if (root) args.push('--root', root)
    // 动手能力默认开（装了就能点）；只想读盘不想让 AI 碰键盘鼠标，就在插件配置里写 input:false
    if (this.config?.input === false) args.push('--no-input')
    this.log?.info?.(`启动 cv sidecar：${this.python} ${args.join(' ')}`)
    const proc = spawn(this.python, args, {
      cwd: HERE,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.proc = proc
    proc.stderr?.on('data', (chunk) => {
      this.stderr = (this.stderr + String(chunk)).slice(-4000)
    })
    const first = await new Promise((resolve, reject) => {
      let buf = ''
      const timer = setTimeout(
        () => reject(new Error(`cv sidecar 启动超时（8s）。stderr：${this.stderr.slice(-600)}`)),
        8000,
      )
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
        reject(new Error(`cv sidecar 启动即退出（code=${code}）。stderr：${this.stderr.slice(-600)}`))
      })
    })
    let info
    try {
      info = JSON.parse(first)
    } catch {
      throw new Error(`cv sidecar 启动输出无法解析：${String(first).slice(0, 300)}`)
    }
    this.port = info.port
    this.screen = info.screen
    this.log?.info?.(`cv sidecar 就绪：端口 ${this.port}，资产目录 ${info.root ?? '(默认)'}`)
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
      throw new Error(`cv sidecar 调用失败（${path}）：${err?.message ?? err}`)
    }
    const text = await res.text()
    let data
    try {
      data = text ? JSON.parse(text) : {}
    } catch {
      throw new Error(`cv sidecar 返回非 JSON（${path}）：${text.slice(0, 300)}`)
    }
    if (!res.ok || data.ok === false) {
      throw new Error(`cv 失败（${path}）：${data.error ?? text.slice(0, 300)}`)
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
      /* 进程可能已经退出 */
    }
  }
}

// ---------------------------------------------------------------- 输出格式

const pct = (v) => `${Math.round((Number(v) || 0) * 100)}%`

function boxText(b) {
  return Array.isArray(b) ? `[${b.join(',')}]` : ''
}

function scanText(d, attach) {
  const lines = []
  lines.push(`扫描 ${d.frame.w}x${d.frame.h}（${d.source?.source === 'file' ? '文件' : `窗口「${d.source?.title ?? ''}」`}）`
    + `，本地提议器给了 ${d.candidates.length} 个候选框，用时 ${d.ms}ms。`)
  lines.push('挑你需要的，用编号编译（不用再描述外观，工具会从框里自己算特征）：')
  for (const c of d.candidates) {
    const bits = [`#${c.n}`, boxText(c.box), c.size, c.color, `边缘${c.edge}`, c.pos]
    lines.push(`  ${bits.filter(Boolean).join('  ')}  ${c.why}`)
  }
  lines.push('下一步：cv_learn{action:"build", packId:"...", picks:[{n:1,id:"portrait",label:"头像"},{n:3,id:"hp",label:"血条"}]}')
  if (attach) lines.push(`（图已附上，候选框已画在图上：${d.frame.path}）`)
  return lines.join('\n')
}

function buildText(d) {
  const lines = []
  lines.push(`${d.append ? '追加' : '新建'}感知包「${d.packId}」，场景「${d.sceneName ?? d.scene ?? ''}」：`
    + `${d.entities.length} 个实体，编译用时 ${d.ms}ms。`)
  for (const e of d.entities) {
    lines.push(`  ${e.id.padEnd(14)} ${boxText(e.box)}${e.risk ? `  ⚠ ${e.risk}` : ''}`)
  }
  const risks = (d.entities || []).filter((e) => e.risk)
  if (risks.length) {
    lines.push('提示：⚠ 的实体在"内容会变"时更容易漏。改标它内部更稳定的子部件，'
      + '或者在内容不同的第二张画面上再 add_scene 一次（工具会顺手校准阈值）。')
  }
  if (!d.scenes?.length || d.scenes.length === 1) {
    lines.push('下一步：切到另一个场景截一帧，cv_learn{action:"add_scene", packId:"' + d.packId + '", scene:"dialog"}，'
      + '工具会用已编好的匹配器自动判"谁在谁不在"，不用模型再看图。')
  }
  return lines.join('\n')
}

function recallText(d, only, boxes) {
  const ents = (d.entities || []).filter((e) => !only || only.includes(e.id))
  const labels = d.labels || {}
  const values = d.values || {}
  const name = (id) => labels[id] || id
  const head = `场景 ${d.scene}（conf ${pct(d.scene_conf)}，距离 ${Number(d.scene_dist).toFixed(3)}）`
    + ` · 变化 ${d.changed < 0 ? '首帧' : `${d.changed} 格`} · ${d.ms}ms`
    + (d.frame ? ` · 帧 ${d.frame}` : '')
  const lines = [head]
  // 没有包不是错误：帧号照样能用（点坐标），顺手读到的文字块也能按文字点。
  if (d.unlabeled) {
    lines.push('  （这个窗口还没有感知包：认不出实体，但帧号和下面的文字块可以拿来点）')
    if (d.hint) lines.push(`  · ${d.hint}`)
  } else if (!ents.length) {
    lines.push(`  包里没有可用实体（packId=${d.packId}）${only ? '，或者 only 把它们都过滤掉了' : ''}。`)
  }
  for (const e of ents) {
    if (!e.present) continue
    const bx = boxes?.[e.id]
    const v = values[e.id]
    lines.push(`  ✓ ${name(e.id)}${v === undefined || v === null ? '' : ` = ${v}`}`
      + `  ${pct(e.conf)}${bx ? ` [${bx.join(',')}]` : ''}`)
  }
  // 被挡住但还认得出来的：单独标出来，免得把"看得见一部分"当成"完好无损"
  const occ = ents.filter((e) => e.present && (d.occluded || []).includes(e.id))
  if (occ.length) {
    lines.push(`  ⛔ 有遮挡：${occ.map((e) => name(e.id)).join('、')}（只看得见一部分，数值/文字可能受影响）`)
  }
  const miss = ents.filter((e) => !e.present)
  if (miss.length) lines.push(`  · 不在：${miss.map((e) => name(e.id)).join('、')}`)
  // 派生数值（血量 = 在场红心数这类）不属于任何单个实体，单独列一行
  const derived = Object.keys(values).filter((k) => !ents.some((e) => e.id === k))
  if (derived.length) {
    lines.push(`  · 数值：${derived.map((k) => `${name(k)} ${values[k]}`).join('  ')}`)
  }
  // 顺手读的文字（cv_recall 的 text 参数）：系统 OCR 读普通文字，读不出就是空
  if (Array.isArray(d.text) && d.text.length) {
    for (const t of d.text) {
      const blk = Array.isArray(t.blocks) ? t.blocks : []
      const where = t.band ? `（${t.band}）` : ''
      if (!t.text) {
        lines.push(`  · 文字${where}：没读出字${t.note ? ` —— ${t.note}` : ''}`)
        continue
      }
      lines.push(`  · 文字${where}：`)
      if (blk.length) {
        for (const b of blk) {
          const val = b.value === undefined ? '' : ` = ${b.value}`
          lines.push(`    「${b.text}」${val}  ${b.kind} [${b.box.join(',')}]`)
        }
      } else {
        lines.push(`    “${t.text}”`)
      }
      if (t.dropped) lines.push(`    （另有 ${t.dropped} 块换缩放读不一致，已丢弃）`)
    }
  }
  // 没编过的新东西（cv_recall 的 novel 参数）：只告诉"哪儿多了块东西"和它的颜色大小
  const nv = Array.isArray(d.novel) ? d.novel : []
  if (nv.length) {
    lines.push(`  · 新东西：${nv.length} 处 —— ` + nv
      .map((it) => `${it.color || ''}色 ${it.box[2]}x${it.box[3]} @ ${it.at[0]}%,${it.at[1]}%`)
      .join('；'))
  } else if (d.novel_note) {
    lines.push(`  · ${d.novel_note}`)
  }
  if (d.stale) {
    lines.push(`⚠ 画面和编译时差太多（距离 ${Number(d.scene_dist).toFixed(3)} > 阈值），`
      + '结果不可信：换主题/分辨率/布局后需要重新 cv_learn{action:"add_scene"}。')
  }
  if (d.scene === 'unknown') lines.push('（场景未收录：这个画面不在包里，实体结果只作参考。）')
  return lines.join('\n')
}

function actText(d) {
  if (!d || d.ok === false) {
    const lines = [`✗ [${d?.code || 'FAILED'}] ${d?.error || '动作没成功（后端没给原因）'}`]
    if (d?.would_click) lines.push(`  （本来想点 (${d.would_click.join(',')})，没发出去）`)
    if (Array.isArray(d?.done) && d.done.length) {
      for (const s of d.done) lines.push(`  ${s.ok ? '✓' : '✗'} 第${s.step}步：${s.did}`)
    }
    return lines.join('\n')
  }
  const lines = []
  if (Array.isArray(d.done)) {
    lines.push(`✓ 批处理 ${d.done.length} 步全部完成 · ${d.ms}ms`)
    for (const s of d.done) lines.push(`  ✓ ${s.did}`)
  } else {
    const tail = []
    if (d.changedTiles !== undefined || d.changed !== undefined) {
      tail.push(`变化 ${d.changedTiles ?? d.changed} 格`)
    }
    if (d.scene) tail.push(`场景 ${d.scene}${d.scene_from ? `（原 ${d.scene_from}）` : ''}`)
    if (d.recovered) tail.push('顺带切了前台')
    tail.push(`${d.ms}ms`)
    lines.push(`✓ ${d.did}${d.target ? `「${d.target}」` : ''} · ${tail.join(' · ')}`)
    if (d.clicked_screen_xy) {
      const reg = (d.changedRegion ?? []).slice(0, 3).map((b) => `[${b.join(',')}]`).join(' ')
      const sp = d.coordSpace === 'client' ? '画面坐标' : '屏幕坐标'
      lines.push(`  · 你给的 ${sp} → 实际点屏幕 (${d.clicked_screen_xy.join(',')})${reg ? `，变的区域 ${reg}` : ''}`)
    }
    if (d.requested_hwnd) {
      const bad = d.foreground_ok === false
      lines.push(
        `  · ${bad ? '⚠ 前台不对' : '前台'}：${d.actual_foreground_title || '无标题'}（${d.actual_foreground_hwnd}）` +
          (bad ? `，请求的是 ${d.requested_hwnd}` : ''),
      )
    }
    if (d.verdict === 'no_change' && d.changedTiles === 0) {
      lines.push('  · 画面没变：可能没点到，或者这个动作本来就不改画面')
    }
    if (d.hint) lines.push(`  · ${d.hint}`)
  }
  if (d.frame) lines.push(`  · 帧 ${d.frame}（下一步直接用它，别再 recall）`)
  if (d.carried) lines.push('  · 这一帧的框是沿用的（画面变动不大）；位置有疑问就再 recall 一次')
  if (d.frame_note) lines.push(`  · ${d.frame_note}`)
  return lines.join('\n')
}

// ---------------------------------------------------------------- 工具

function packParam(required = false) {
  return {
    type: 'string',
    description: required
      ? '感知包 id（cv_learn{action:"list"} 里能看到）'
      : '感知包 id（省略则用唯一那个包）',
  }
}

function srcParams(properties) {
  return {
    ...properties,
    hwnd: { type: 'number', description: '窗口句柄（省略 = 前台窗口）' },
    title: { type: 'string', description: '窗口标题片段（hwnd 的替代写法）' },
    path: { type: 'string', description: '直接从 png 文件读（调试/离线验收用）' },
  }
}

export function apply(ctx, config) {
  const log = ctx.logger?.('cv-recall')
  const side = new Sidecar(log, config ?? {})

  ctx.effect(() => () => side.stop())

  ctx.inject(['tools'], (toolsCtx) => {
    const tools = toolsCtx.tools

    // ------------------------------------------------------------ cv_learn
    tools.register({
      name: 'cv_learn',
      description:
        '编译一次，之后免费用：把游戏画面里的实体（血条/小地图/技能图标/对话框…）编译成常驻的本地视觉资产。\n'
        + '· scan：截一帧，本地算法免费提议候选框（不用你看图）\n'
        + '· build：用候选编号（或坐标）把实体编译进包；一个场景标一次，多个场景 append 进同一个包\n'
        + '· add_scene：再加一个场景，并用这一帧顺手校准"谁在谁不在"的阈值（不用再标一次）\n'
        + '· list/get/delete：管理包\n'
        + '编译只需要坐标 + 名字：颜色、纹理、形状特征全部由本地代码从框内像素自己算，模型不必描述外观。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: {
            type: 'string',
            enum: ['scan', 'build', 'add_scene', 'list', 'get', 'delete', 'windows'],
          },
          packId: packParam(false),
          game: { type: 'string', description: '游戏名（build 时用，省略 = packId）' },
          scene: { type: 'string', description: '场景名（battle/dialog/choice…）' },
          picks: {
            type: 'array',
            description: 'build 时标了哪些实体。用 {n:候选编号, id, label} 或 {id, box:[x,y,w,h], search:[x,y,w,h]}',
            items: {
              type: 'object',
              additionalProperties: true,
              properties: {
                n: { type: 'number', description: 'scan 返回的候选编号' },
                id: { type: 'string', description: '实体 id（血条用 hp_frame 这种稳定的名字）' },
                label: { type: 'string', description: '给人看的名字' },
                kind: { type: 'string', description: '类别：bar/icon/panel/icon-list/text…' },
                box: { type: 'array', items: { type: 'number' }, description: '绝对坐标 [x,y,w,h]' },
                search: {
                  type: 'array',
                  items: { type: 'number' },
                  description: '搜索窗 [x,y,w,h]：会移动的实体（光标/角色）必须给，省略 = 框周围留边',
                },
                digits: {
                  type: 'object',
                  additionalProperties: true,
                  description: '这一格是数字时给：{box:[x,y,w,h], value:"00"}（value = 这一帧上的'
                    + '真实数字，工具会顺手学下这些字形；之后帧不用再看模型就能读数值）',
                },
                text: {
                  type: 'object',
                  additionalProperties: true,
                  description: '这一格是不定长的文字时给：{box:[x,y,w,h], value:"BOMB"}。'
                    + '和 digits 同一套机制，只是输出留字符串（不参与求和）。'
                    + '要求这块字高度一致、字之间有间隙（词/数字串都行，混大小写和冒号这类符号不行）',
                },
              },
              required: ['id'],
            },
          },
          present: {
            type: 'array',
            items: { type: 'string' },
            description: 'add_scene 时：这个场景里「应该在」的实体 id（只会放宽它们的阈值）',
          },
          absent: {
            type: 'array',
            items: { type: 'string' },
            description: 'add_scene 时：这个场景里「肯定不在」的实体 id（只会收严它们的阈值）',
          },
          append: { type: 'boolean', description: 'build 时并入已有包（多场景编译用）' },
          derived: {
            type: 'array',
            description: '派生数值：不直接出现在画面上、靠已编实体算出来的数（血量=在场红心数）。'
              + '[{id:"hp", label:"血量", from:["heart_1","heart_2","heart_3"], agg:"count"}]',
            items: { type: 'object', additionalProperties: true },
          },
          note: { type: 'string', description: '备注：版本/分辨率/主题' },
          attach: { type: 'boolean', description: 'scan 时把候选框画在图上给模型看（要花视觉 token，默认不画）' },
          limit: { type: 'number', description: 'scan 候选数上限（默认 12）' },
          ...srcParams({}),
        },
        required: ['action'],
      },
      output: FRAME_OUTPUT,
      async execute(args) {
        const a = args?.action
        if (a === 'windows') {
          const d = await side.call('/windows')
          const rows = (d.windows ?? []).slice(0, 12)
          return {
            text: ['可见窗口（前台在前）：', ...rows.map((w, i) =>
              `${i + 1}. 「${w.title}」 ${w.rect?.[2]}x${w.rect?.[3]} hwnd=${w.hwnd}${w.fg ? ' ←前排' : ''}`)].join('\n'),
          }
        }
        if (a === 'scan') {
          const body = { hwnd: args?.hwnd, title: args?.title, path: args?.path, limit: args?.limit }
          const d = await side.call('/scan', body)
          let image
          if (args?.attach) {
            const attachments = ctx.get?.('attachments')
            if (attachments?.saveImage && d.frame?.path && existsSync(d.frame.path)) {
              const ref = await attachments.saveImage({
                data: new Uint8Array(readFileSync(d.frame.path)),
                mediaType: 'image/png',
                name: 'cv-scan.png',
              })
              image = ref
            }
          }
          return { text: scanText(d, Boolean(args?.attach)), ...(image ? { image } : {}) }
        }
        if (a === 'build') {
          const d = await side.call('/build', {
            hwnd: args?.hwnd, title: args?.title, path: args?.path,
            packId: args?.packId, game: args?.game, scene: args?.scene,
            picks: args?.picks, append: Boolean(args?.append), note: args?.note,
            derived: args?.derived,
            present: args?.present,
          })
          return { text: buildText(d) }
        }
        if (a === 'add_scene') {
          const d = await side.call('/scene', {
            hwnd: args?.hwnd, title: args?.title, path: args?.path,
            packId: args?.packId, scene: args?.scene,
            present: args?.present, absent: args?.absent,
          })
          const on = Object.entries(d.presence || {}).filter(([, v]) => v).map(([k]) => k)
          const off = Object.entries(d.presence || {}).filter(([, v]) => !v).map(([k]) => k)
          const relaxed = Object.entries(d.tau || {}).map(([k, v]) =>
            `${k}:g${Number(v?.ncc_g ?? 0).toFixed(2)}/e${Number(v?.ncc_e ?? 0).toFixed(2)}`)
          return {
            text: [
              `场景「${d.scene}」已加入包「${d.packId}」，共 ${d.scenes?.length ?? 1} 个场景。`,
              `这一帧里判为在：${on.join(', ') || '（无）'}`,
              off.length ? `判为不在：${off.join(', ')}` : '',
              relaxed.length ? `阈值：${relaxed.join('  ')}` : '',
              '（只截一帧就够了：位置固定、外观由模板判，模型不用再看图。）',
            ].filter(Boolean).join('\n'),
          }
        }
        if (a === 'list') {
          const d = await side.call('/list', {})
          if (!d.packs?.length) {
            return { text: `还没有感知包（资产目录 ${d.root}）。先 cv_learn{action:"scan"} 截一帧。` }
          }
          return {
            text: ['已编好的感知包：', ...d.packs.map((p) =>
              `· ${p.packId}（${p.game}）${p.entities} 个实体，场景：${(p.scenes || []).join('/') || '（无）'}`)].join('\n'),
          }
        }
        if (a === 'get') {
          const d = await side.call('/get', { packId: args?.packId })
          const lines = [`包「${d.packId}」（${d.game}）画面 ${d.frame?.[0]}x${d.frame?.[1]}，场景：${(d.scenes || []).join('/')}`]
          for (const e of d.entities || []) {
            const t = d.tau?.[e.id] || {}
            const w = d.w?.[e.id] || {}
            const ws = Object.entries(w).map(([k, v]) => `${k}:${Number(v).toFixed(2)}`).join(' ')
            lines.push(`  ${e.id.padEnd(14)} ${boxText(e.box)}  ${e.label ?? ''} 门限 ${Object.entries(t)
              .map(([k, v]) => `${k}:${Number(v).toFixed(2)}`).join(' ')} 权重 ${ws}`)
          }
          return { text: lines.join('\n') }
        }
        if (a === 'delete') {
          const d = await side.call('/delete', { packId: args?.packId })
          return { text: `已删除感知包「${d.deleted}」。` }
        }
        throw new Error(`未知 action：${a}`)
      },
    })

    // ------------------------------------------------------------ cv_recall
    tools.register({
      name: 'cv_recall',
      description:
        '用已编译的感知包读当前画面：0 token、几十毫秒，返回场景 / 哪些实体在 / 位置 / 置信度 / 画面变了几格。\n'
        + '这是"看清盘面"的便宜路径：每回合先调它；只有它报 stale 或场景 unknown 时才需要截图问模型。\n'
        + '不需要任何参数（默认用当前前台窗口和唯一的包）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          packId: packParam(false),
          only: {
            type: 'array',
            items: { type: 'string' },
            description: '只看这些实体（省 token）',
          },
          detail: { type: 'boolean', description: '带上每路特征的分数（调试用）' },
          boxes: { type: 'boolean', description: '带上实体框坐标' },
          values: {
            type: 'boolean',
            description: '读数值：编译时给实体标了 digits（数字框 + 当时的数字）才会读，'
              + '默认开；关了可以省几毫秒',
          },
          labels: { type: 'boolean', description: '输出实体的中文描述（默认开）' },
          text: {
            anyOf: [
              { type: 'boolean', description: 'true = 读下半屏（对话框/选项都在下面）' },
              { type: 'string', description: 'bottom / top / mid / full：读哪一条横带' },
              {
                type: 'array',
                items: {},
                description: '[x,y,w,h] 或 {box:[x,y,w,h], label:"名字", alphabet:"digits"}，最多 6 块',
              },
            ],
            description: '顺手读文字（系统 OCR，中文也认；数字给 alphabet:"digits" 走内置字形模型）。'
              + '不给就不读——读文字要 100~200ms，别每帧都开',
          },
          ...srcParams({}),
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const d = await side.call('/recall', {
          packId: args?.packId, hwnd: args?.hwnd, title: args?.title, path: args?.path,
          detail: Boolean(args?.detail), boxes: Boolean(args?.boxes),
          values: args?.values !== false, labels: args?.labels !== false,
          text: args?.text,
        })
        return { text: recallText(d, args?.only, d.boxes) }
      },
    })

    // ------------------------------------------------------------ cv_act
    tools.register({
      name: 'cv_act',
      description:
        '动手：点、打字、按键、滚动、拖动、一串动作。**目标用 id 或文字指路，不要给坐标**——\n'
        + 'cv_recall 回执里每个实体/文字块都有 id（文字块 t1/t2…），顶层有「帧 f17」；把它传进 frame，\n'
        + '再说 click:"t2" 或 click:"继续"，我负责在按下那一刻现算屏幕坐标、检查有没有被别的窗口挡住、'
        + '点完再看画面变了几格。\n'
        + '典型两步循环（0 张截图）：cv_recall{text:"bottom"} → cv_act{frame:"f17", click:"继续"}。\n'
        + '不要为了点一下就重新 cv_recall：回执里已经给了新帧号，接着用就行。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: {
            type: 'string',
            enum: ['click', 'double_click', 'right_click', 'key', 'type', 'scroll', 'drag', 'batch', 'stop'],
            description: '要做什么，默认 click；stop 会中止批处理并放开所有按住的键',
          },
          frame: {
            type: 'string',
            description: '帧号（cv_recall 回执里的 f17 这种）。省略就用这个窗口最近一帧',
          },
          click: {
            anyOf: [
              { type: 'string', description: '实体 id / 文字块 id（t2）/ 文字（继续）/ 中文描述（血量）' },
              { type: 'object', description: '{x,y}：客户区像素坐标（自己算好了才用）' },
            ],
            description: '点哪儿（click/double_click/right_click/scroll/drag 用）',
          },
          key: { type: 'string', description: '按键或组合键，如 space / esc / w / ctrl+shift+t' },
          repeat: { type: 'integer', description: 'key：按几次' },
          hold_ms: { type: 'integer', description: 'key/click：按住多少毫秒（长按）' },
          text: { type: 'string', description: 'action=type：要打的字（中文也行）' },
          delay_ms: { type: 'integer', description: 'type：每个字之间等多少毫秒（应用吃键盘太快时用）' },
          direction: { type: 'string', enum: ['up', 'down'], description: 'scroll：滚哪边' },
          clicks: { type: 'integer', description: 'scroll：滚几格' },
          from: { anyOf: [{ type: 'string' }, { type: 'object' }], description: 'drag：起点（id/文字/{x,y}）' },
          to: { anyOf: [{ type: 'string' }, { type: 'object' }], description: 'drag：终点' },
          steps: {
            type: 'array',
            items: { type: 'object' },
            description: 'action=batch：动作数组，每项同本工具参数（{click:"继续"}、{key:"space",repeat:3}）。'
              + '任一步失败就停，并把原因原样返回',
          },
          pause_ms: { type: 'integer', description: 'batch：每步之间等多少毫秒，默认 60' },
          deadline_ms: { type: 'integer', description: 'batch：整批最多花多少毫秒，默认 8000' },
          verify: {
            type: 'string',
            enum: ['changed', 'scene', 'none'],
            description: '动作后看一眼：changed=只比画面变了几格（快）；scene=再跑一遍感知包拿场景/实体；'
              + 'none=不看（动画面、或后面马上要 recall 时用）',
          },
          settle_ms: { type: 'integer', description: '动作后等画面稳定多久再看，默认 200' },
          recover: { type: 'boolean', description: '窗口不在前台时是否接着试着切过去（默认会先抢一次前台）' },
          move_settle_ms: {
            type: 'integer',
            description: '指针移到目标后等多久再按下（让 hover 生效），默认 0；吃 hover 的按钮给 250',
          },
          down_up_ms: { type: 'integer', description: '按下到抬起的间隔，默认 0（游戏按钮建议 ≥80）' },
          force_foreground: {
            type: 'boolean',
            description: '动手前是否抢前台，默认 true。给 false 时只认"已经在前台且没被挡住"，否则拒绝',
          },
          coordSpace: {
            type: 'string',
            enum: ['screen', 'client'],
            description: '裸坐标 {x,y} / "925,512" 属于哪个坐标系：screen（默认，屏幕绝对坐标，和 coach_* 一致）'
              + ' 或 client（画面/客户区坐标，就是 cv_recall 回的 box 那套）。按 id/文字/描述指路时不受影响',
          },
          allow_windows: {
            type: 'array',
            items: { type: 'integer' },
            description: '允许点到的其它窗口句柄（弹出的对话框等）；不给则被任何别的窗口盖住就拒绝',
          },
          dry_run: { type: 'boolean', description: '只算坐标和校验，不真的动手（排障用）' },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const d = await side.call('/act', {
          action: args?.action || 'click',
          frame: args?.frame,
          click: args?.click,
          key: args?.key,
          repeat: args?.repeat,
          hold_ms: args?.hold_ms,
          text: args?.text,
          delay_ms: args?.delay_ms,
          direction: args?.direction,
          clicks: args?.clicks,
          from: args?.from,
          to: args?.to,
          steps: args?.steps,
          pause_ms: args?.pause_ms,
          deadline_ms: args?.deadline_ms,
          verify: args?.verify,
          settle_ms: args?.settle_ms,
          move_settle_ms: args?.move_settle_ms,
          down_up_ms: args?.down_up_ms,
          force_foreground: args?.force_foreground,
          coordSpace: args?.coordSpace ?? args?.coord_space,
          recover: Boolean(args?.recover),
          allow_windows: args?.allow_windows,
          dry_run: Boolean(args?.dry_run),
        })
        return { text: actText(d) }
      },
    })

    log?.info?.('cv-recall 已注册：cv_learn（编译）/ cv_recall（读盘）/ cv_act（动手）')
  })
}
