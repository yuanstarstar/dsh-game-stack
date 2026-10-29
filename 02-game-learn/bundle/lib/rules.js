/**
 * @local/dsh-rule-workbench —— 通用规则工作台
 *
 * 思路：模型的输出不是「动作」，而是「规则」；动作由引擎按规则算出来。
 * 于是每个决策都能追到 rule_id + 条件求值 trace + 事实新鲜度，可复现、可回放、可回滚。
 *
 * 这一层是通用的：不绑定任何游戏。规则只认识 JSON 事实（facts），
 * 游戏、桌面自动化、任何有状态流程都能用同一套规则。
 *
 * 工具：rules_doc / rules_list / rules_save / rules_delete / rules_toggle /
 *       rules_run / rules_replay / rules_stats / rules_outcome / rules_results
 * 面板：侧边栏「规则工作台」（规则表 / 编辑 / 测试台 / 决策与结果 / 覆盖率）
 *
 * 决策不只有一条：引擎返回「选定 + 预期结果 + 备选方案」，
 * 结果可用 rules_outcome 回填，累积成每条规则的真实胜率。
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'rule-workbench'

const MAX_RULES = 2000
const MAX_UNKNOWN = 400
const MAX_DECISIONS = 500
const MAX_SESSIONS = 600
const ALT_LIMIT = 3
const OUTCOMES = new Set(['win', 'loss', 'neutral'])

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string' } },
    required: ['text'],
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

// ------------------------------------------------------------------ 规则引擎

const OPS = {
  '==': (a, b) => a === b,
  '!=': (a, b) => a !== b,
  '<': (a, b) => Number(a) < Number(b),
  '<=': (a, b) => Number(a) <= Number(b),
  '>': (a, b) => Number(a) > Number(b),
  '>=': (a, b) => Number(a) >= Number(b),
  in: (a, b) => Array.isArray(b) && b.includes(a),
  contains: (a, b) => (Array.isArray(a) ? a.includes(b) : String(a ?? '').includes(String(b))),
  matches: (a, b) => new RegExp(String(b)).test(String(a ?? '')),
  truthy: (a) => Boolean(a),
  falsy: (a) => !a,
}

/** 感知层落盘的 facts 文件（coach_facts 写这里）。 */
const COACH_FACTS_FILE = () => join(homedir(), '.dsh', 'coach-perceive', 'last-facts.json')

/**
 * 取这一帧的事实：优先 facts，其次 factsPath，再次 useCoachFacts（读感知层落盘文件）。
 * 文件读不到 / 不是 JSON 都直接报错——绝不拿空对象冒充一帧事实。
 */
function resolveFacts(args) {
  const a = args && typeof args === 'object' ? args : {}
  const wantsFile = a.useCoachFacts === true || (typeof a.factsPath === 'string' && a.factsPath.trim() !== '')
  if (!wantsFile) return a.facts && typeof a.facts === 'object' ? a.facts : {}
  const file = a.useCoachFacts === true ? COACH_FACTS_FILE() : String(a.factsPath).trim()
  if (!existsSync(file)) throw new Error(`facts 文件不存在：${file}（先让感知层调 coach_facts 落盘）`)
  const raw = readFileSync(file, 'utf8')
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch (error) {
    throw new Error(`facts 文件不是合法 JSON：${file}（${error?.message ?? error}）`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`facts 文件内容不是对象：${file}`)
  const savedAt = typeof parsed.__savedAt === 'string' ? Date.parse(parsed.__savedAt) : Number.NaN
  if (Number.isFinite(savedAt)) parsed.__ageMs = Math.max(0, Math.round(Date.now() - savedAt))
  return parsed
}

/** 点路径取值，支持数组下标：enemies.0.hp；返回 found 表示「事实是否存在」——不存在绝不假设为 0。 */
function readPath(facts, path) {
  const parts = String(path).split('.').filter((p) => p.length > 0)
  let cur = facts
  for (const part of parts) {
    if (cur === null || cur === undefined) return { found: false, value: undefined }
    if (Array.isArray(cur)) {
      const idx = Number(part)
      if (!Number.isInteger(idx) || idx < 0 || idx >= cur.length) return { found: false, value: undefined }
      cur = cur[idx]
      continue
    }
    if (typeof cur !== 'object' || !(part in cur)) return { found: false, value: undefined }
    cur = cur[part]
  }
  return { found: true, value: cur }
}

function nodeIsGroup(node) {
  return node && typeof node === 'object' && !Array.isArray(node) && ('all' in node || 'any' in node || 'not' in node)
}

function evalNode(node, facts, trace, depth = 0) {
  if (depth > 12) return { ok: false, note: '条件嵌套过深' }
  if (!node || typeof node !== 'object' || Array.isArray(node)) return { ok: false, note: '条件格式不对' }

  if ('all' in node) {
    const list = Array.isArray(node.all) ? node.all : []
    const notes = []
    for (const child of list) {
      const r = evalNode(child, facts, trace, depth + 1)
      if (!r.ok) {
        notes.push(r.note ?? '不满足')
        return { ok: false, note: notes.join('；') || 'all 不满足' }
      }
    }
    return { ok: true, note: `all(${list.length})` }
  }
  if ('any' in node) {
    const list = Array.isArray(node.any) ? node.any : []
    const notes = []
    for (const child of list) {
      const r = evalNode(child, facts, trace, depth + 1)
      if (r.ok) return { ok: true, note: `any(${list.length})` }
      notes.push(r.note ?? '不满足')
    }
    return { ok: false, note: notes.join(' / ') || 'any 全不满足' }
  }
  if ('not' in node) {
    const r = evalNode(node.not, facts, trace, depth + 1)
    return { ok: !r.ok, note: `not(${r.note ?? ''})` }
  }
  if ('exists' in node) {
    const paths = Array.isArray(node.exists) ? node.exists : [node.exists]
    for (const p of paths) {
      const got = readPath(facts, p)
      if (!got.found) {
        trace.push({ cond: `exists(${p})`, value: '(缺失)', ok: false })
        return { ok: false, note: `缺少事实 ${p}` }
      }
    }
    return { ok: true, note: `exists(${paths.join(',')})` }
  }

  if ('path' in node) {
    const got = readPath(facts, node.path)
    if (!got.found) {
      trace.push({ cond: `${node.path} ${node.op ?? '=='} ${JSON.stringify(node.value)}`, value: '(缺失)', ok: false })
      return { ok: false, note: `缺少事实 ${node.path}` }
    }
    const op = String(node.op ?? '==')
    const fn = OPS[op]
    if (!fn) {
      trace.push({ cond: `${node.path} ${op} ...`, value: '(未知操作符)', ok: false })
      return { ok: false, note: `未知操作符 ${op}` }
    }
    let ok = false
    try {
      ok = Boolean(fn(got.value, node.value))
    } catch (error) {
      return { ok: false, note: `求值失败：${String(error?.message ?? error)}` }
    }
    trace.push({ cond: `${node.path} ${op} ${JSON.stringify(node.value)}`, value: got.value, ok })
    return { ok, note: ok ? 'ok' : `${node.path} 不满足 ${op} ${JSON.stringify(node.value)}（实际 ${JSON.stringify(got.value)}）` }
  }

  // 简写：{ "scene": "battle", "enemy.hp": 12 } 等价于逐条 ==
  const entries = Object.entries(node)
  if (!entries.length) return { ok: true, note: '空条件' }
  for (const [path, value] of entries) {
    const got = readPath(facts, path)
    if (!got.found) {
      trace.push({ cond: `${path} == ${JSON.stringify(value)}`, value: '(缺失)', ok: false })
      return { ok: false, note: `缺少事实 ${path}` }
    }
    const ok = got.value === value
    trace.push({ cond: `${path} == ${JSON.stringify(value)}`, value: got.value, ok })
    if (!ok) return { ok: false, note: `${path} != ${JSON.stringify(value)}（实际 ${JSON.stringify(got.value)}）` }
  }
  return { ok: true, note: 'ok' }
}

/** 预期结果：这条规则命中后预计会发生什么（给人看 + 给回填结果做对照）。 */
function normalizeExpect(input, errors) {
  if (input === undefined || input === null) return null
  if (typeof input !== 'object' || Array.isArray(input)) {
    errors.push('expect 必须是对象，例如 {summary:"预计打掉 12 血", risk:"低"}')
    return null
  }
  const out = {}
  if (input.summary !== undefined) out.summary = String(input.summary).slice(0, 300)
  if (input.risk !== undefined) out.risk = String(input.risk).slice(0, 80)
  if (input.note !== undefined) out.note = String(input.note).slice(0, 300)
  if (input.changes !== undefined) {
    if (!input.changes || typeof input.changes !== 'object' || Array.isArray(input.changes)) {
      errors.push('expect.changes 必须是对象（字段 → 变化量），例如 {"enemy.hp": -12}')
    } else out.changes = input.changes
  }
  for (const [key, value] of Object.entries(input)) {
    if (key in out) continue
    if (typeof value === 'string') out[key] = value.slice(0, 200)
    else if (typeof value === 'number' || typeof value === 'boolean') out[key] = value
    else if (value && typeof value === 'object' && JSON.stringify(value).length < 2000) out[key] = value
  }
  return Object.keys(out).length ? out : null
}

function normalizeRule(input) {
  const errors = []
  if (!input || typeof input !== 'object' || Array.isArray(input)) return { error: '规则必须是对象' }
  const rule = { ...input }
  rule.id = String(rule.id ?? '').trim()
  if (!rule.id) errors.push('缺少 id')
  if (rule.id && !/^[A-Za-z0-9_.:-]{2,80}$/.test(rule.id)) errors.push('id 只能用字母数字和 _ . : -（2~80 字符）')
  rule.priority = Number.isFinite(Number(rule.priority)) ? Number(rule.priority) : 50
  rule.enabled = rule.enabled !== false
  if (rule.when === undefined) errors.push('缺少 when（条件）')
  else if (!rule.when || typeof rule.when !== 'object') errors.push('when 必须是对象')
  if (rule.do === undefined) errors.push('缺少 do（动作）')
  else if (!Array.isArray(rule.do) || !rule.do.length) errors.push('do 必须是非空数组')
  else {
    for (const [i, action] of rule.do.entries()) {
      if (!action || typeof action !== 'object' || Array.isArray(action)) errors.push(`do[${i}] 必须是对象`)
      else if (!action.type) errors.push(`do[${i}] 缺少 type`)
    }
  }
  rule.requires = Array.isArray(rule.requires) ? rule.requires.map(String) : []
  rule.why = String(rule.why ?? '')
  rule.expect = normalizeExpect(rule.expect, errors)
  rule.maxFactAgeMs = Number.isFinite(Number(rule.maxFactAgeMs)) ? Number(rule.maxFactAgeMs) : 0
  return errors.length ? { error: errors.join('；') } : { rule }
}

/** 把 expect 渲染成一行中文（面板和工具输出共用同一套说法）。 */
function expectText(expect) {
  if (!expect || typeof expect !== 'object') return ''
  const parts = []
  if (expect.summary) parts.push(String(expect.summary))
  if (expect.changes && typeof expect.changes === 'object') {
    const delta = Object.entries(expect.changes)
      .map(([k, v]) => `${k}${typeof v === 'number' && v > 0 ? '+' : ''}${v}`)
      .join('，')
    if (delta) parts.push(`预计变化：${delta}`)
  }
  if (expect.risk) parts.push(`风险：${expect.risk}`)
  if (expect.note) parts.push(String(expect.note))
  return parts.join('｜')
}

function newDecisionId() {
  return `d_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
}

/** 按 facts 跑一遍规则集：返回唯一决策 + 预期结果 + 备选方案 + 完整 trace。 */
function runRules(rules, facts, options = {}) {
  const started = Date.now()
  const scope = String(options.scope ?? '')
  const altLimit = Number.isFinite(Number(options.altLimit)) ? Math.max(0, Number(options.altLimit)) : ALT_LIMIT
  const factAge = Number(facts?.__ageMs ?? 0)
  const tried = []
  const matched = []
  const sorted = [...rules].filter((r) => r.enabled !== false).sort((a, b) => {
    const p = (Number(b.priority) || 0) - (Number(a.priority) || 0)
    return p !== 0 ? p : String(a.id).localeCompare(String(b.id))
  })

  for (const rule of sorted) {
    const trace = []
    if (rule.maxFactAgeMs > 0 && factAge > rule.maxFactAgeMs) {
      tried.push({ rule: rule.id, reason: `事实过期 ${factAge}ms > ${rule.maxFactAgeMs}ms`, trace })
      continue
    }
    const missing = []
    for (const path of rule.requires ?? []) {
      if (!readPath(facts, path).found) missing.push(path)
    }
    if (missing.length) {
      tried.push({ rule: rule.id, reason: `缺少前置事实：${missing.join(', ')}`, trace })
      continue
    }
    const result = evalNode(rule.when, facts, trace)
    if (!result.ok) {
      tried.push({ rule: rule.id, reason: result.note, trace })
      continue
    }
    matched.push({ rule, trace })
  }

  if (!matched.length) {
    return {
      ok: false,
      scope,
      id: '',
      decision: { type: 'unknown' },
      actions: [],
      rule: null,
      trace: [],
      alternatives: [],
      matchedCount: 0,
      tried,
      factAgeMs: factAge,
      fallback: true,
      reason: sorted.length ? '没有规则匹配当前事实' : '该域还没有任何规则',
      ms: Date.now() - started,
    }
  }

  const winner = matched[0].rule
  const alternatives = matched.slice(1, 1 + altLimit).map((entry, i) => ({
    rule: entry.rule.id,
    priority: entry.rule.priority,
    decision: entry.rule.do[0],
    actions: entry.rule.do,
    why: entry.rule.why,
    expect: entry.rule.expect ?? null,
    rank: i + 2,
    skippedBecause:
      Number(entry.rule.priority) === Number(winner.priority)
        ? `优先级相同（${winner.priority}），按 id 排序让给 ${winner.id}`
        : `优先级 ${entry.rule.priority} 低于 ${winner.priority}（${winner.id}）`,
  }))

  return {
    ok: true,
    scope,
    id: newDecisionId(),
    at: new Date().toISOString(),
    decision: winner.do[0],
    actions: winner.do,
    rule: winner.id,
    priority: winner.priority,
    why: winner.why,
    expect: winner.expect ?? null,
    trace: matched[0].trace,
    alternatives,
    matchedCount: matched.length,
    tried,
    factAgeMs: factAge,
    fallback: false,
    ms: Date.now() - started,
  }
}

// ---------------------------------------------------------------------- 存储

function dshHome() {
  return process.env.DSH_HOME || join(homedir(), '.dsh')
}

function safeScope(scope) {
  const raw = String(scope ?? '').trim()
  if (!raw) throw new Error('需要 scope（规则域，例如游戏名或流程名）')
  return raw.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 80)
}

class Store {
  constructor(logger) {
    this.logger = logger
    this.root = join(dshHome(), 'rule-workbench')
  }

  dir(scope) {
    return join(this.root, safeScope(scope))
  }

  file(scope, name) {
    return join(this.dir(scope), name)
  }

  ensure(scope) {
    const dir = this.dir(scope)
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
    return dir
  }

  readJson(file, fallback) {
    try {
      if (!existsSync(file)) return fallback
      const raw = readFileSync(file, 'utf8').replace(/^\uFEFF/, '')
      return raw.trim() ? JSON.parse(raw) : fallback
    } catch (error) {
      this.logger?.warn?.(`[rule-workbench] 读取失败 ${file}: ${String(error?.message ?? error)}`)
      return fallback
    }
  }

  writeJson(file, value) {
    const dir = join(file, '..')
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
    const tmp = `${file}.tmp`
    writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
    renameSync(tmp, file)
  }

  rules(scope) {
    const data = this.readJson(this.file(scope, 'rules.json'), { rules: [] })
    return Array.isArray(data.rules) ? data.rules : []
  }

  saveRules(scope, rules) {
    this.ensure(scope)
    this.writeJson(this.file(scope, 'rules.json'), { version: 1, scope: safeScope(scope), rules })
  }

  stats(scope) {
    return this.readJson(this.file(scope, 'stats.json'), { runs: 0, matched: 0, unmatched: 0, hits: {}, lastHit: {} })
  }

  saveStats(scope, stats) {
    this.ensure(scope)
    this.writeJson(this.file(scope, 'stats.json'), stats)
  }

  recordRun(scope, result) {
    const stats = this.stats(scope)
    stats.runs = (stats.runs ?? 0) + 1
    if (result.ok) {
      stats.matched = (stats.matched ?? 0) + 1
      stats.hits = stats.hits ?? {}
      stats.hits[result.rule] = (stats.hits[result.rule] ?? 0) + 1
      stats.lastHit = stats.lastHit ?? {}
      stats.lastHit[result.rule] = new Date().toISOString()
    } else {
      stats.unmatched = (stats.unmatched ?? 0) + 1
    }
    this.saveStats(scope, stats)
    return stats
  }

  appendUnknown(scope, entry) {
    const file = this.file(scope, 'unknown.jsonl')
    const line = `${JSON.stringify(entry)}\n`
    try {
      const existing = existsSync(file) ? readFileSync(file, 'utf8').split('\n').filter(Boolean) : []
      existing.push(line.trim())
      const keep = existing.slice(-MAX_UNKNOWN)
      writeFileSync(file, `${keep.join('\n')}\n`, 'utf8')
    } catch (error) {
      this.logger?.warn?.(`[rule-workbench] 记录未覆盖回合失败：${String(error?.message ?? error)}`)
    }
  }

  unknowns(scope, limit = 20) {
    const file = this.file(scope, 'unknown.jsonl')
    const rows = []
    try {
      if (!existsSync(file)) return rows
      return readFileSync(file, 'utf8')
        .split('\n')
        .filter(Boolean)
        .slice(-Math.max(1, limit))
        .map((line) => JSON.parse(line))
        .reverse()
    } catch {
      return rows
    }
  }

  /** 决策台账：只由 rules_run / 面板运行写入（回放不写，避免刷屏）。 */
  recordDecision(scope, result) {
    this.ensure(scope)
    const file = this.file(scope, 'decisions.jsonl')
    const entry = {
      id: result.id,
      at: result.at,
      scope: safeScope(scope),
      rule: result.rule,
      decision: result.decision,
      actions: result.actions,
      expect: result.expect ?? null,
      alternatives: (result.alternatives ?? []).map((a) => ({ rule: a.rule, priority: a.priority, decision: a.decision })),
      factAgeMs: result.factAgeMs,
      outcome: '',
      note: '',
      closedAt: '',
    }
    try {
      const lines = existsSync(file) ? readFileSync(file, 'utf8').split('\n').filter(Boolean) : []
      lines.push(JSON.stringify(entry))
      writeFileSync(file, `${lines.slice(-MAX_DECISIONS).join('\n')}\n`, 'utf8')
    } catch (error) {
      this.logger?.warn?.(`[rule-workbench] 记录决策失败：${String(error?.message ?? error)}`)
    }
    return entry
  }

  decisions(scope, limit = 20) {
    const file = this.file(scope, 'decisions.jsonl')
    try {
      if (!existsSync(file)) return []
      return readFileSync(file, 'utf8')
        .split('\n')
        .filter(Boolean)
        .slice(-Math.max(1, limit))
        .map((line) => JSON.parse(line))
        .reverse()
    } catch {
      return []
    }
  }

  // ---- 流程状态机（flow）与会话录制（sessions） ----

  flow(scope) {
    return this.readJson(this.file(scope, 'flow.json'), null)
  }

  saveFlow(scope, flow) {
    this.ensure(scope)
    this.writeJson(this.file(scope, 'flow.json'), flow)
  }

  learnedFlow(scope) {
    return this.readJson(this.file(scope, 'flow.learned.json'), null)
  }

  saveLearnedFlow(scope, flow) {
    this.ensure(scope)
    this.writeJson(this.file(scope, 'flow.learned.json'), flow)
  }

  appendSession(scope, entry) {
    this.ensure(scope)
    const file = this.file(scope, 'sessions.jsonl')
    try {
      const lines = existsSync(file) ? readFileSync(file, 'utf8').split('\n').filter(Boolean) : []
      lines.push(JSON.stringify(entry))
      writeFileSync(file, `${lines.slice(-MAX_SESSIONS).join('\n')}\n`, 'utf8')
    } catch (error) {
      this.logger?.warn?.(`[rule-workbench] 记录会话失败：${String(error?.message ?? error)}`)
    }
    return entry
  }

  sessions(scope, limit = MAX_SESSIONS) {
    const file = this.file(scope, 'sessions.jsonl')
    try {
      if (!existsSync(file)) return []
      return readFileSync(file, 'utf8')
        .split('\n')
        .filter(Boolean)
        .slice(-Math.max(1, limit))
        .map((line) => JSON.parse(line))
    } catch {
      return []
    }
  }

  sessionCount(scope) {
    try {
      const file = this.file(scope, 'sessions.jsonl')
      if (!existsSync(file)) return 0
      return readFileSync(file, 'utf8').split('\n').filter(Boolean).length
    } catch {
      return 0
    }
  }

  /** 回填「决策之后实际发生了什么」。同一条可改判（会先撤掉旧统计）。 */  closeOutcome(scope, id, outcome, note = '') {
    if (!OUTCOMES.has(outcome)) throw new Error(`outcome 只能是 win / loss / neutral（收到 ${outcome}）`)
    this.ensure(scope)
    const file = this.file(scope, 'decisions.jsonl')
    const lines = existsSync(file) ? readFileSync(file, 'utf8').split('\n').filter(Boolean) : []
    if (!lines.length) throw new Error('这个域还没有记录过决策，先 rules_run 一次再回填结果')
    const rows = lines.map((line) => JSON.parse(line))
    const target = String(id ?? '').trim()
    const idx = !target || target === 'latest' ? rows.length - 1 : rows.map((row) => row.id).lastIndexOf(target)
    if (idx < 0) throw new Error(`找不到决策 ${target}（用 rules_results 看最近的决策ID）`)
    const row = rows[idx]
    if (row.outcome) this.bumpOutcome(scope, row.rule, row.outcome, -1)
    row.outcome = outcome
    row.note = String(note ?? '').slice(0, 300)
    row.closedAt = new Date().toISOString()
    rows[idx] = row
    writeFileSync(file, `${rows.slice(-MAX_DECISIONS).map((item) => JSON.stringify(item)).join('\n')}\n`, 'utf8')
    this.bumpOutcome(scope, row.rule, outcome, 1)
    return row
  }

  bumpOutcome(scope, ruleId, outcome, delta) {
    const stats = this.stats(scope)
    stats.outcomes = stats.outcomes ?? { win: 0, loss: 0, neutral: 0 }
    stats.outcomes[outcome] = Math.max(0, (stats.outcomes[outcome] ?? 0) + delta)
    stats.ruleOutcomes = stats.ruleOutcomes ?? {}
    const per = stats.ruleOutcomes[ruleId] ?? { win: 0, loss: 0, neutral: 0 }
    per[outcome] = Math.max(0, (per[outcome] ?? 0) + delta)
    stats.ruleOutcomes[ruleId] = per
    this.saveStats(scope, stats)
    return stats
  }

  scopes() {
    try {
      if (!existsSync(this.root)) return []
      return readdirSync(this.root, { withFileTypes: true })
        .filter((d) => d.isDirectory())
        .map((d) => d.name)
    } catch {
      return []
    }
  }
}

// -------------------------------------------------------- 流程状态机（自动反推）

/** 拉平叶子路径（数组最多看前 4 项），用来描述「这一帧有哪些字段」。 */
function leafPaths(value, prefix = '', depth = 0, out = []) {
  if (depth > 3) {
    out.push(prefix || '(root)')
    return out
  }
  if (value === null || value === undefined || typeof value !== 'object') {
    out.push(prefix || '(root)')
    return out
  }
  if (Array.isArray(value)) {
    if (!value.length) out.push(prefix || '(root)')
    else value.slice(0, 4).forEach((item, i) => leafPaths(item, `${prefix}.${i}`, depth + 1, out))
    return out
  }
  const keys = Object.keys(value)
  if (!keys.length) out.push(prefix || '(root)')
  for (const key of keys) leafPaths(value[key], prefix ? `${prefix}.${key}` : key, depth + 1, out)
  return out
}

/** 短字符串叶子（界面标题这类稳定文本），用于当阶段的判别条件。 */
function smallStrings(value, prefix = '', depth = 0, out = []) {
  if (depth > 2 || value === null || value === undefined) return out
  if (typeof value === 'string') {
    const text = value.trim()
    if (prefix && text.length >= 2 && text.length <= 24) out.push({ path: prefix, value: text })
    return out
  }
  if (Array.isArray(value)) {
    value.slice(0, 4).forEach((item, i) => smallStrings(item, `${prefix}.${i}`, depth + 1, out))
    return out
  }
  if (typeof value === 'object') {
    for (const key of Object.keys(value)) smallStrings(value[key], prefix ? `${prefix}.${key}` : key, depth + 1, out)
  }
  return out
}

function phaseSignature(facts) {
  const scene = typeof facts?.scene === 'string' ? facts.scene : ''
  const keys = [...new Set(leafPaths(facts ?? {}))].sort()
  return { scene, keys }
}

function jaccard(a, b) {
  const setA = new Set(a)
  const setB = new Set(b)
  if (!setA.size && !setB.size) return 1
  let inter = 0
  for (const item of setA) if (setB.has(item)) inter += 1
  return inter / (setA.size + setB.size - inter)
}

function condCount(node) {
  if (!node || typeof node !== 'object') return 0
  if (Array.isArray(node.all)) return node.all.reduce((sum, child) => sum + condCount(child), 0)
  if (Array.isArray(node.any)) return node.any.reduce((sum, child) => sum + condCount(child), 0)
  if (node.not) return condCount(node.not)
  if ('exists' in node || 'path' in node) return 1
  return Object.keys(node).length
}

/** 拿一帧事实判断处在流程的哪个阶段；不做唯一化，多命中交给调用方标「歧义」。 */
function detectPhase(flow, facts) {
  const phases = Array.isArray(flow?.phases) ? flow.phases : []
  if (!phases.length) {
    return { ok: false, unknown: true, reason: '还没有流程文件（先 flow_learn 学一份，或 flow_save 手写）', matches: [], legal: [], needReads: [], missing: [] }
  }
  const matches = []
  for (const [index, phase] of phases.entries()) {
    const trace = []
    const r = evalNode(phase.when, facts ?? {}, trace)
    if (r.ok) matches.push({ phase, index, spec: condCount(phase.when), trace })
  }
  if (!matches.length) {
    return { ok: false, unknown: true, reason: '没有阶段条件成立（新的界面状态？用 flow_learn 反推或 flow_save 补一个阶段）', matches: [], legal: [], needReads: [], missing: [] }
  }
  matches.sort((a, b) => b.spec - a.spec || a.index - b.index)
  const best = matches[0]
  const needReads = Array.isArray(best.phase.needReads) ? best.phase.needReads : []
  const missing = needReads.filter((p) => !readPath(facts ?? {}, p).found)
  return {
    ok: true,
    unknown: false,
    phase: best.phase.id,
    label: best.phase.label ?? '',
    confidence: Math.max(0.2, Math.min(0.95, Number(best.phase.confidence ?? 0.6) / (matches.length > 1 ? 1.4 : 1))),
    legal: Array.isArray(best.phase.legal) ? best.phase.legal : [],
    needReads,
    missing,
    ambiguous: matches.slice(1).map((m) => m.phase.id),
    matches: matches.map((m) => ({ id: m.phase.id, spec: m.spec })),
  }
}

/** 软提醒：从不阻断，只把风险摆出来（用户选的策略）。 */
function softWarnings({ card, facts, action }) {
  const list = []
  if (card?.unknown) {
    list.push(`流程未识别：${card.reason}`)
  }
  if (card?.ambiguous?.length) {
    list.push(`阶段有歧义：同时成立 ${card.ambiguous.join('、')}，按条件更具体的一条判定（如判错请补判别条件）`)
  }
  for (const path of card?.missing ?? []) list.push(`缺少事实 ${path}：涉及它的规则这一轮可能误判`)
  const legal = card?.legal ?? []
  const act = action && typeof action === 'object' ? (action.type ?? action.action ?? '') : action
  if (act && legal.length && !legal.includes(String(act))) {
    list.push(`动作「${act}」不在本阶段合法动作内（合法：${legal.join(' / ')}）——仍可执行，但先确认是不是阶段判错了`)
  }
  const age = Number(facts?.__ageMs ?? 0)
  if (age > 15000) list.push(`事实已 ${Math.round(age / 1000)}s 旧：回合制里旧盘面等于错盘面`)
  return list
}

function phaseLabelFrom(texts) {
  const stable = texts.find((item) => item.path.includes('title') || item.path.includes('header') || item.path.includes('name'))
  return stable ? String(stable.value).slice(0, 18) : ''
}

/** 从录制到的会话步骤里反推流程：按「场景 + 字段结构」聚类，判别条件用稳定文本与字段有无。 */
function learnFlow(rows, options = {}) {
  const minSupport = Math.max(1, Number(options.minSupport ?? 2))
  const steps = (Array.isArray(rows) ? rows : []).filter((row) => row && row.facts && typeof row.facts === 'object')
  if (!steps.length) {
    return { ok: false, reason: '没有可用的会话步骤：先用 turn_card / rules_run 打几步（会自动录进 sessions.jsonl）' }
  }

  const clusters = []
  for (const [index, step] of steps.entries()) {
    const sig = phaseSignature(step.facts)
    let target = clusters.find((c) => c.scene === sig.scene && jaccard(c.keys, sig.keys) >= 0.9)
    if (!target) {
      target = { scene: sig.scene, keys: sig.keys, steps: [] }
      clusters.push(target)
    }
    target.keys = [...new Set([...target.keys, ...sig.keys])].sort()
    target.steps.push({ index, step, texts: smallStrings(step.facts ?? {}) })
  }

  const kept = clusters
    .filter((c) => c.steps.length >= minSupport)
    .sort((a, b) => a.steps[0].index - b.steps[0].index)
  const dropped = clusters.filter((c) => c.steps.length < minSupport)

  const phases = []
  for (const [order, cluster] of kept.entries()) {
    const others = kept.filter((c) => c !== cluster)
    // 稳定文本：在本簇 ≥70% 出现，且在别的簇里出现率 <30%
    const counts = new Map()
    for (const item of cluster.steps) {
      const seen = new Map()
      for (const text of item.texts) if (!seen.has(text.path)) seen.set(text.path, text.value)
      for (const [path, value] of seen) {
        const key = `${path}\u0000${value}`
        counts.set(key, (counts.get(key) ?? 0) + 1)
      }
    }
    const stableTexts = []
    for (const [key, n] of counts) {
      const [path, value] = key.split('\u0000')
      if (n / cluster.steps.length < 0.7) continue
      const elsewhere = others.length
        ? others.filter((other) => other.steps.some((item) => item.texts.some((t) => t.path === path && t.value === value))).length / others.length
        : 0
      if (elsewhere < 0.3) stableTexts.push({ path, value, hit: n })
    }
    stableTexts.sort((a, b) => b.hit - a.hit)

    const clusterKeys = new Set(cluster.keys)
    const foreignKeys = [...new Set(others.flatMap((c) => c.keys))].filter((key) => !clusterKeys.has(key))
    const mustHave = [...clusterKeys].filter((key) => cluster.steps.every((item) => phaseSignature(item.step.facts).keys.includes(key))).slice(0, 4)
    const mustNot = foreignKeys
      .filter((key) => cluster.steps.every((item) => !phaseSignature(item.step.facts).keys.includes(key)))
      .slice(0, 2)

    const whenParts = []
    if (cluster.scene) whenParts.push({ path: 'scene', op: '==', value: cluster.scene })
    for (const text of stableTexts.slice(0, 2)) whenParts.push({ path: text.path, op: 'contains', value: text.value })
    for (const key of mustHave) whenParts.push({ exists: [key] })
    for (const key of mustNot) whenParts.push({ not: { exists: [key] } })

    const actions = [...new Set(cluster.steps.map((item) => item.step.action).filter(Boolean).map((a) => (typeof a === 'object' ? a.type ?? a.action ?? JSON.stringify(a) : String(a))))]
    const support = cluster.steps.length
    const discriminators = stableTexts.length + mustNot.length
    const confidence = Math.max(0.25, Math.min(0.95, 0.4 + 0.08 * Math.min(support, 6) + (discriminators ? 0.12 : 0)))

    phases.push({
      id: `${(cluster.scene || 'phase').replace(/[^\w.-]+/g, '_')}-${order + 1}`,
      label: phaseLabelFrom(stableTexts) || '',
      scene: cluster.scene,
      when: whenParts.length ? { all: whenParts } : { all: [] },
      legal: actions,
      needReads: mustHave,
      support,
      confidence: Number(confidence.toFixed(2)),
      discriminators,
      firstSeen: cluster.steps[0].step.at ?? '',
    })
  }

  const flow = {
    version: 1,
    scope: String(options.scope ?? ''),
    draft: true,
    learnedAt: new Date().toISOString(),
    source: { steps: steps.length, minSupport, clusters: clusters.length },
    entry: phases[0]?.id ?? '',
    phases,
  }

  // 阶段转移（相邻两步的相位变化）与覆盖率自检
  const transitions = []
  let prev = ''
  let known = 0
  let ambiguous = 0
  for (const step of steps) {
    const hit = detectPhase(flow, step.facts)
    if (hit.ok) {
      known += 1
      if (hit.ambiguous?.length) ambiguous += 1
      if (prev && prev !== hit.phase) {
        const row = transitions.find((t) => t.from === prev && t.to === hit.phase)
        if (row) row.count += 1
        else transitions.push({ from: prev, to: hit.phase, count: 1 })
      }
      prev = hit.phase
    }
  }

  return {
    ok: true,
    flow,
    report: {
      steps: steps.length,
      clusters: clusters.length,
      kept: kept.length,
      dropped: dropped.map((c) => ({ scene: c.scene, steps: c.steps.length })),
      coverage: steps.length ? Number(((known / steps.length) * 100).toFixed(1)) : 0,
      ambiguous,
      transitions,
    },
  }
}

function flowText(flow) {
  if (!flow?.phases?.length) return '（还没有流程文件）'
  const lines = [`流程「${flow.scope || ''}」共 ${flow.phases.length} 个阶段${flow.draft ? '（草稿，来自自动反推）' : ''}｜学习于 ${flow.learnedAt ?? '手写'}`]
  for (const phase of flow.phases) {
    lines.push(
      `  ${phase.id}${phase.label ? `（${phase.label}）` : ''}  支持 ${phase.support ?? '-'} 步｜置信 ${phase.confidence ?? '-'}｜合法动作 ${(phase.legal ?? []).join('/') || '未记录'}`,
    )
  }
  return lines.join('\n')
}

// ------------------------------------------------------------------ DSL 帮助

const DSL_DOC = [
  '## 规则工作台 DSL（v2）',
  '规则 = { id, priority, enabled, when, requires, do, why, expect, maxFactAgeMs }',
  '',
  '**when 条件**（四种写法可任意嵌套）：',
  '- 简写比较：{"scene": "battle", "enemy.hp": 12}   ← 逐条 ==',
  '- 显式比较：{"path": "enemy.hp", "op": "<=", "value": 20}',
  '- 组合：{"all": [ ... ]} / {"any": [ ... ]} / {"not": { ... }}',
  '- 存在性：{"exists": ["enemy.hp", "self.light"]}',
  '',
  '**op 全集**：== != < <= > >= in contains matches truthy falsy',
  '**路径**：点号取值，支持数组下标（enemies.0.hp）；取不到 = 事实缺失 = 条件不成立（不会当成 0）',
  '',
  '**requires**：执行前必须先存在的事实；缺一个这条规则就直接不匹配（并记原因）',
  '**do**：非空动作数组，每条要有 type；引擎返回 do[0] 作为决策，do 全量留给执行层',
  '**expect**（强烈建议写）：这条规则命中后「预计发生什么」，供人事后对照。',
  '  { "summary": "预计打掉 12 血", "changes": {"enemy.hp": -12}, "risk": "低", "note": "对方没闪避就稳赚" }',
  '**maxFactAgeMs**：事实超过这个年龄就不许用（防拿旧盘面做决策；facts.__ageMs 提供年龄）',
  '**why**：一句话理由，面板可见，供人审',
  '',
  '**决策返回（v2）**：',
  '{ ok, id, decision, actions, rule, priority, why, expect,',
  '  alternatives:[{ rank, rule, priority, decision, actions, why, expect, skippedBecause }],',
  '  matchedCount, trace:[{cond,value,ok}], tried:[{rule,reason}], factAgeMs, ms }',
  '- **selected**：命中的最高优先级规则就是决策；',
  '- **alternatives**：同时成立的其余规则（默认 3 条），每条带「为什么没选它」（优先级低/同优先级按 id 让位）；',
  '- **unknown**：一条都没命中 → ok:false，写进 unknown.jsonl 供复盘写新规则。',
  '',
  '**结果回填（闭环）**：',
  '- rules_run 返回 `id`（d_xxx）→ 事后用 rules_outcome{outcome:"win|loss|neutral", note} 回填；',
  '- 同一 id 可改判（旧统计会自动撤销）；胜率累加到每条规则，rules_results 里看得见；',
  '- 预期落空时先改 facts/条件，再谈换决策——不要把结论写进规则。',
].join('\n')

// ---------------------------------------------------------------------- 插件

export function apply(ctx, config) {
  const cfg = { ...(config ?? {}) }
  const logger = ctx.logger?.('rule-workbench') ?? ctx.logger
  const store = new Store(logger)
  const apiRoot = '/rule-workbench'

  const listPayload = (scope) => {
    const rules = store.rules(scope)
    const stats = store.stats(scope)
    return {
      scope,
      file: store.file(scope, 'rules.json'),
      rules: rules.map((rule) => ({
        id: rule.id,
        priority: rule.priority,
        enabled: rule.enabled !== false,
        why: rule.why,
        expect: rule.expect ?? null,
        hits: stats.hits?.[rule.id] ?? 0,
        lastHit: stats.lastHit?.[rule.id] ?? '',
        outcomes: stats.ruleOutcomes?.[rule.id] ?? { win: 0, loss: 0, neutral: 0 },
        when: rule.when,
        requires: rule.requires,
        do: rule.do,
      })),
      stats: { runs: stats.runs ?? 0, matched: stats.matched ?? 0, unmatched: stats.unmatched ?? 0 },
      outcomes: stats.outcomes ?? { win: 0, loss: 0, neutral: 0 },
      decisions: store.decisions(scope, 8),
      flow: (() => {
        const learned = flowOf(scope)
        return learned
          ? { phases: learned.phases?.length ?? 0, draft: Boolean(learned.draft), learnedAt: learned.learnedAt ?? '', entry: learned.entry ?? '', steps: store.sessionCount(scope) }
          : { phases: 0, draft: false, learnedAt: '', entry: '', steps: store.sessionCount(scope) }
      })(),
      scopes: store.scopes(),
    }
  }

  if (!cfg.scope) cfg.scope = 'default'

  const flowOf = (scope) => store.flow(scope) ?? store.learnedFlow(scope)
  const recordStep = (scope, facts, extra = {}) =>
    store.appendSession(scope, { at: new Date().toISOString(), scope: safeScope(scope), facts: facts ?? {}, ...extra })
  const renderWarnings = (warnings) => (warnings?.length ? ['', '⚠ 软提醒（不阻断执行）：', ...warnings.map((w) => `  - ${w}`)] : [])

  ctx.inject(['tools'], (toolsCtx) => {
    const tools = toolsCtx.tools

    tools.register({
      name: 'rules_doc',
      description: '规则工作台 DSL 速查：条件写法、op 全集、requires/do/why/maxFactAgeMs 语义、决策返回结构。动手写规则前先读它。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: TEXT_OUTPUT,
      async execute() {
        return { text: DSL_DOC }
      },
    })

    tools.register({
      name: 'rules_list',
      description: '列出某个规则域（scope，例如游戏名）的全部规则：id/优先级/启用/命中次数/最后命中/条件/动作，以及已知 scope 列表。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string', description: '规则域；省略用配置里的默认域' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const payload = listPayload(scope)
        if (!payload.rules.length) {
          const hint = payload.scopes.length ? `已知域：${payload.scopes.join('、')}` : '还没有任何域'
          return { text: `域「${scope}」还没有规则（${hint}）。用 rules_save 写入第一条。` }
        }
        const lines = [
          `域「${scope}」共 ${payload.rules.length} 条规则（文件 ${payload.file}）`,
          `运行统计：${payload.stats.runs} 次 / 命中 ${payload.stats.matched} / 未覆盖 ${payload.stats.unmatched}`,
          '',
          ...payload.rules.map(
            (r) =>
              `${r.enabled ? '●' : '○'} [${r.priority}] ${r.id}  命中 ${r.hits}${r.lastHit ? `（最后 ${r.lastHit}）` : ''}\n    when: ${JSON.stringify(r.when)}\n    do:   ${JSON.stringify(r.do[0])}${r.why ? `\n    为什么：${r.why}` : ''}`,
          ),
        ]
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'rules_save',
      description:
        '写入/更新规则（按 id 覆盖，可传单条或数组）。会先做结构与动作校验，校验不过直接返回错误清单，不会写坏文件。这是「让 AI 学会某局面」的唯一入口。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string', description: '规则域；省略用默认域' },
          rules: {
            type: 'array',
            description: '规则数组（或直接给单条规则对象）',
            items: { type: 'object', additionalProperties: true },
          },
        },
        required: ['rules'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const incoming = Array.isArray(args?.rules) ? args.rules : [args?.rules]
        if (!incoming.length) throw new Error('rules 不能为空')
        const normalized = []
        const errors = []
        for (const [i, raw] of incoming.entries()) {
          const { rule, error } = normalizeRule(raw)
          if (error) errors.push(`第 ${i + 1} 条：${error}`)
          else normalized.push(rule)
        }
        if (errors.length) return { text: `规则未写入，校验没通过：\n${errors.map((e) => `- ${e}`).join('\n')}` }

        const existing = store.rules(scope)
        const byId = new Map(existing.map((rule) => [rule.id, rule]))
        for (const rule of normalized) byId.set(rule.id, rule)
        const merged = [...byId.values()]
        if (merged.length > MAX_RULES) return { text: `规则太多（${merged.length} > ${MAX_RULES}），先删一些再写。` }
        store.saveRules(scope, merged)

        const conflicts = []
        const seen = new Map()
        for (const rule of merged) {
          const key = `${rule.priority}|${JSON.stringify(rule.when)}`
          if (seen.has(key)) conflicts.push(`${seen.get(key)} 与 ${rule.id} 同优先级同条件`)
          else seen.set(key, rule.id)
        }
        return {
          text: [
            `已写入 ${normalized.length} 条到域「${scope}」（现在共 ${merged.length} 条）`,
            normalized.map((r) => `+ [${r.priority}] ${r.id}`).join('\n'),
            conflicts.length ? `\n⚠ 可能冲突：${conflicts.join('；')}` : '',
            `\n用 rules_run 立刻验证：rules_run{scope:"${scope}", facts:{...}}`,
          ]
            .filter(Boolean)
            .join('\n'),
        }
      },
    })

    tools.register({
      name: 'rules_delete',
      description: '删除某个域里的一条规则（按 id）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          id: { type: 'string', description: '规则 id' },
        },
        required: ['id'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const id = String(args.id)
        const existing = store.rules(scope)
        const next = existing.filter((rule) => rule.id !== id)
        if (next.length === existing.length) return { text: `域「${scope}」没有规则 ${id}` }
        store.saveRules(scope, next)
        return { text: `已删除 ${id}（域「${scope}」剩 ${next.length} 条）` }
      },
    })

    tools.register({
      name: 'rules_toggle',
      description: '启用/停用一条规则（A/B 对比用，不用删）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          id: { type: 'string' },
          enabled: { type: 'boolean', description: 'true 启用 / false 停用' },
        },
        required: ['id', 'enabled'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const id = String(args.id)
        const existing = store.rules(scope)
        const target = existing.find((rule) => rule.id === id)
        if (!target) return { text: `域「${scope}」没有规则 ${id}` }
        target.enabled = Boolean(args.enabled)
        store.saveRules(scope, existing)
        return { text: `${id} 现在${target.enabled ? '启用' : '停用'}（域「${scope}」）` }
      },
    })

    tools.register({
      name: 'rules_run',
      description:
        '拿一帧事实（facts JSON）跑规则引擎：返回唯一决策 + 该决策的预期结果 + 若干备选方案（附带「为什么没选它」）+ 完整条件求值 trace + 每条没匹配上的原因。决策内容可复现（同一帧跑 100 次同一个决策）；每次运行生成决策ID，事后可用 rules_outcome 回填真实结果。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          facts: { type: 'object', additionalProperties: true, description: '这一帧的事实，例如 {scene:"battle", enemy:{hp:12}, self:{light:3}}；也可改用 factsPath / useCoachFacts 从文件读' },
          factsPath: { type: 'string', description: '从 JSON 文件读事实（感知层落盘的文件路径）' },
          useCoachFacts: { type: 'boolean', description: '直接读感知层 coach_facts 落盘的 last-facts.json' },
          record: { type: 'boolean', description: '是否计入统计、决策台账与未覆盖队列，默认 true' },
          alternatives: { type: 'number', description: `最多给几条备选方案，默认 ${ALT_LIMIT}` },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const facts = resolveFacts(args)
        const rules = store.rules(scope)
        const result = runRules(rules, facts, { scope, altLimit: args?.alternatives })
        const flow = flowOf(scope)
        const hit = flow ? detectPhase(flow, facts) : null
        if (hit) {
          result.phase = hit.unknown ? '' : hit.phase
          result.warnings = softWarnings({ card: hit, facts, action: result.decision })
        }
        if (args?.record !== false) {
          store.recordRun(scope, result)
          if (result.ok) store.recordDecision(scope, result)
          else store.appendUnknown(scope, { at: new Date().toISOString(), scope, facts, tried: result.tried })
          if (hit) recordStep(scope, facts, { kind: 'decision', phase: result.phase, action: result.ok ? result.decision : null, rule: result.rule ?? '' })
        }
        const lines = []
        if (result.ok) {
          lines.push(`决策：${JSON.stringify(result.decision)}`)
          lines.push(`规则：${result.rule}（优先级 ${result.priority}）${result.why ? `｜为什么：${result.why}` : ''}`)
          const expect = expectText(result.expect)
          lines.push(`预期结果：${expect || '（这条规则没写 expect；建议补上，事后好对照）'}`)
          if (result.id) lines.push(`决策ID：${result.id}  ← 事后用 rules_outcome{scope:"${scope}", outcome:"win|loss|neutral"} 回填`)
          if (result.phase) lines.push(`流程阶段：${result.phase}${flow ? '' : '（无流程文件）'}`)
          if (result.warnings?.length) lines.push(...renderWarnings(result.warnings))
          if (result.alternatives?.length) {
            lines.push('', `备选方案（另有 ${result.alternatives.length} 条规则同时成立）：`)
            for (const alt of result.alternatives) {
              lines.push(`  ${alt.rank}) [${alt.priority}] ${alt.rule} → ${JSON.stringify(alt.decision)}`)
              lines.push(`     ${alt.skippedBecause}`)
              if (alt.why) lines.push(`     为什么：${alt.why}`)
              const altExpect = expectText(alt.expect)
              if (altExpect) lines.push(`     预期：${altExpect}`)
            }
          } else {
            lines.push('', '备选方案：无（当前事实下只有这一条规则成立）')
          }
          lines.push(`耗时 ${result.ms}ms｜事实年龄 ${result.factAgeMs}ms｜可复现（同帧同决策）`)
        } else {
          lines.push('决策：unknown（没有规则匹配）')
          lines.push(`原因：${result.reason}`)
          lines.push(`耗时 ${result.ms}ms｜事实年龄 ${result.factAgeMs}ms`)
        }
        if (result.trace.length) {
          lines.push('', '条件求值：')
          for (const t of result.trace) lines.push(`  ${t.ok ? '✓' : '✗'} ${t.cond} → ${JSON.stringify(t.value)}`)
        }
        const misses = result.tried.filter((t) => t.reason !== 'ok')
        if (!result.ok && misses.length) {
          lines.push('', '没匹配上的规则与其他原因：')
          for (const t of misses.slice(0, 8)) lines.push(`  · ${t.rule}：${t.reason}`)
          if (misses.length > 8) lines.push(`  … 还有 ${misses.length - 8} 条`)
        }
        if (result.ok && result.tried.length) {
          lines.push('', `另有 ${result.tried.length} 条规则没成立（看 rules_stats 或单独跑一帧排查）`)
        }
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'rules_outcome',
      description:
        '回填「决策之后实际发生了什么」：win/loss/neutral，可选一句 note 说清为什么。按决策ID定位（省略=最近一次）。结果会累加进每条规则的胜率，让规则能按实战淘汰，而不是靠猜。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          id: { type: 'string', description: '决策ID（rules_run 返回的 d_xxx；省略或 latest = 最近一条）' },
          outcome: { type: 'string', enum: ['win', 'loss', 'neutral'], description: '实际结果' },
          note: { type: 'string', description: '一句话说明（例如「拼点输了，剩 3 血」）' },
        },
        required: ['outcome'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const row = store.closeOutcome(scope, args?.id ?? 'latest', String(args.outcome), args?.note ?? '')
        const stats = store.stats(scope)
        const per = stats.ruleOutcomes?.[row.rule] ?? { win: 0, loss: 0, neutral: 0 }
        const total = per.win + per.loss + per.neutral
        const rate = total ? ((per.win / total) * 100).toFixed(0) : '0'
        return {
          text: [
            `已记录：${row.id}`,
            `决策：${JSON.stringify(row.decision)}｜规则：${row.rule}`,
            `结果：${row.outcome}${row.note ? `（${row.note}）` : ''}`,
            `该规则战绩：${per.win} 胜 / ${per.loss} 负 / ${per.neutral} 平（胜率 ${rate}%，共 ${total} 次）`,
            row.expect ? `原预期：${expectText(row.expect)}` : '原预期：（未写）',
            '',
            row.expect && row.outcome === 'loss' ? '→ 预期落空的，建议回看 facts 与条件，改规则而不是改结论。' : '',
          ]
            .filter(Boolean)
            .join('\n'),
        }
      },
    })

    tools.register({
      name: 'rules_results',
      description: '看决策台账：最近若干次「决策 + 预期 + 实际结果」，以及每条规则的真实胜率。用来判断哪条规则该改、该停用。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          limit: { type: 'number', description: '看最近多少条决策，默认 10' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const rows = store.decisions(scope, Number(args?.limit ?? 10))
        const stats = store.stats(scope)
        if (!rows.length) return { text: `域「${scope}」还没有决策记录，先 rules_run 跑一帧。` }
        const lines = [`域「${scope}」最近 ${rows.length} 次决策：`]
        for (const row of rows) {
          const mark = row.outcome ? { win: '✓胜', loss: '✗负', neutral: '·平' }[row.outcome] : '？未回填'
          lines.push('')
          lines.push(`${row.at} ${mark}  ${row.id}`)
          lines.push(`  决策：${JSON.stringify(row.decision)}｜规则：${row.rule}`)
          if (row.expect) lines.push(`  预期：${expectText(row.expect)}`)
          if (row.alternatives?.length) lines.push(`  当时备选：${row.alternatives.map((a) => `${a.rule}[${a.priority}]`).join('、')}`)
          if (row.note) lines.push(`  备注：${row.note}`)
        }
        const per = Object.entries(stats.ruleOutcomes ?? {}).filter(([, v]) => v.win + v.loss + v.neutral > 0)
        if (per.length) {
          lines.push('', '规则战绩（按胜率）：')
          for (const [ruleId, v] of per.sort((a, b) => b[1].win / Math.max(1, b[1].win + b[1].loss) - a[1].win / Math.max(1, a[1].win + a[1].loss))) {
            const total = v.win + v.loss + v.neutral
            lines.push(`  ${ruleId}：${v.win}胜 ${v.loss}负 ${v.neutral}平（${((v.win / total) * 100).toFixed(0)}%）`)
          }
        } else {
          lines.push('', '还没有回填过结果（rules_outcome）——没结果就没法判断规则好坏。')
        }
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'rules_replay',
      description:
        '回放一批帧（frames 事实数组）：逐帧跑引擎，返回每帧决策与覆盖率统计（命中率、未覆盖帧、各规则命中分布）。用来验证「新规则会不会打破老局面」。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          frames: { type: 'array', description: '事实数组，顺序即时间顺序', items: { type: 'object', additionalProperties: true } },
          limit: { type: 'number', description: '最多回放多少帧，默认 200' },
        },
        required: ['frames'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const frames = Array.isArray(args?.frames) ? args.frames.slice(0, Math.max(1, Number(args?.limit ?? 200))) : []
        if (!frames.length) return { text: 'frames 为空' }
        const rules = store.rules(scope)
        if (!rules.length) return { text: `域「${scope}」还没有规则，先 rules_save。` }
        const hits = {}
        const unknown = []
        const started = Date.now()
        for (const [i, facts] of frames.entries()) {
          const result = runRules(rules, facts ?? {}, { scope })
          if (result.ok) hits[result.rule] = (hits[result.rule] ?? 0) + 1
          else unknown.push(i)
        }
        const matched = frames.length - unknown.length
        const pct = ((matched / frames.length) * 100).toFixed(1)
        const lines = [
          `回放 ${frames.length} 帧，总耗时 ${Date.now() - started}ms`,
          `覆盖率 ${pct}%（命中 ${matched} / 未覆盖 ${unknown.length}）`,
          '',
          ...Object.entries(hits)
            .sort((a, b) => b[1] - a[1])
            .map(([id, n]) => `  ${id}：${n} 帧（${((n / frames.length) * 100).toFixed(1)}%）`),
          unknown.length ? `\n未覆盖帧下标：${unknown.slice(0, 40).join(',')}${unknown.length > 40 ? ' …' : ''}` : '',
        ]
        return { text: lines.filter(Boolean).join('\n') }
      },
    })

    tools.register({
      name: 'rules_stats',
      description: '看某个域的运行统计（跑了几次、命中/未覆盖、每条规则命中数）与最近的未覆盖回合（含当时的完整 facts，可直接拿来写新规则）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          unknownLimit: { type: 'number', description: '看最近多少条未覆盖回合，默认 5' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const stats = store.stats(scope)
        const rules = store.rules(scope)
        const rows = Object.entries(stats.hits ?? {}).sort((a, b) => b[1] - a[1])
        const unknowns = store.unknowns(scope, Number(args?.unknownLimit ?? 5))
        const lines = [
          `域「${scope}」：${rules.length} 条规则`,
          `运行 ${stats.runs ?? 0} 次｜命中 ${stats.matched ?? 0}｜未覆盖 ${stats.unmatched ?? 0}`,
        ]
        const out = stats.outcomes ?? { win: 0, loss: 0, neutral: 0 }
        const outTotal = (out.win ?? 0) + (out.loss ?? 0) + (out.neutral ?? 0)
        if (outTotal) {
          lines.push(`结果回填 ${outTotal} 次：${out.win} 胜 / ${out.loss} 负 / ${out.neutral} 平（胜率 ${((out.win / outTotal) * 100).toFixed(0)}%）`)
        } else if ((stats.matched ?? 0) > 0) {
          lines.push('⚠ 还没有回填过结果：决策好不好无从判断，用 rules_outcome 补。')
        }
        if (rows.length) {
          lines.push('', '规则命中分布：')
          for (const [id, n] of rows) {
            const per = stats.ruleOutcomes?.[id]
            const battle = per && per.win + per.loss + per.neutral > 0 ? `｜战绩 ${per.win}胜${per.loss}负${per.neutral}平` : ''
            lines.push(`  ${id}：${n} 次${stats.lastHit?.[id] ? `（最后 ${stats.lastHit[id]}）` : ''}${battle}`)
          }
        }
        if (unknowns.length) {
          lines.push('', `最近 ${unknowns.length} 个未覆盖回合（挑一个写规则）：`)
          for (const row of unknowns) {
            lines.push(`  · ${row.at}  facts=${JSON.stringify(row.facts).slice(0, 300)}`)
            const tries = (row.tried ?? []).slice(0, 3).map((t) => `${t.rule}:${t.reason}`)
            if (tries.length) lines.push(`      没匹配原因：${tries.join('；')}`)
          }
        }
        return { text: lines.join('\n') }
      },
    })
    tools.register({
      name: 'turn_card',
      description:
        '拿一帧事实生成「回合卡」：当前流程阶段 + 合法动作 + 需要读懂但缺失的字段 + 软提醒。同时把这一步录进 sessions.jsonl（flow_learn 的样本来源）。回合制里每步先调它，别靠自己回忆流程。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          facts: { type: 'object', additionalProperties: true, description: '这一帧的事实（同 rules_run 的 facts）；也可改用 factsPath / useCoachFacts' },
          factsPath: { type: 'string', description: '从 JSON 文件读事实（感知层落盘的文件路径）' },
          useCoachFacts: { type: 'boolean', description: '直接读感知层 coach_facts 落盘的 last-facts.json' },
          action: { type: 'object', additionalProperties: true, description: '可选：你打算/已经做的动作，用来检查是否合法（不在合法列表只提醒，不拦截）' },
          note: { type: 'string', description: '可选：一句备注（例如「等他确认」）' },
        },
        required: [],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const facts = resolveFacts(args)
        const flow = flowOf(scope)
        const hit = flow ? detectPhase(flow, facts) : detectPhase(null, facts)
        const seq = store.sessionCount(scope) + 1
        const history = store.sessions(scope, 3)
        const prev = history.length ? history[history.length - 1] : null
        const warnings = softWarnings({ card: hit, facts, action: args?.action })
        if (!flow) warnings.push('这个域还没有流程文件：flow_learn 可以从已录的步骤反推，或 flow_save 手写')
        const lines = [
          `【回合卡 #${seq}】${scope} / 阶段：${hit.unknown ? '未识别' : `${hit.phase}${hit.label ? `（${hit.label}）` : ''}`}${hit.ok ? `｜置信 ${hit.confidence}` : ''}`,
          flow ? `流程来源：flow.json（${flow.source?.steps ?? '?'} 步学习，${flow.phases?.length ?? 0} 个阶段）` : '流程来源：（空）',
        ]
        if (prev) {
          const prevAct = prev.action ? `｜上一步动作 ${typeof prev.action === 'object' ? prev.action.type ?? JSON.stringify(prev.action) : prev.action}` : ''
          lines.push(`上一卡：${prev.phase || '未识别'}${prevAct}`)
        }
        lines.push(`合法动作：${hit.legal?.length ? hit.legal.join(' / ') : '未记录（先在规则里记动作，或 flow_save 补 legal）'}`)
        if (hit.needReads?.length) lines.push(`本阶段应读懂：${hit.needReads.map((p) => (hit.missing.includes(p) ? `${p}✗` : `${p}✓`)).join(' / ')}`)
        lines.push('', ...renderWarnings(warnings))
        recordStep(scope, facts, { kind: 'card', phase: hit.phase ?? '', action: args?.action ?? null, note: args?.note ?? '', warnings })
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'flow_get',
      description: '看某个域的流程状态机：不带 facts 列出全部阶段（合法动作/支持步数/置信）；带 facts 则判断当前处在哪个阶段并给出卡片要点。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          facts: { type: 'object', additionalProperties: true, description: '可选：这一帧事实，给了就顺带判定当前阶段' },
          factsPath: { type: 'string', description: '可选：从 JSON 文件读事实' },
          useCoachFacts: { type: 'boolean', description: '可选：直接读感知层 coach_facts 落盘的 last-facts.json' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const flow = flowOf(scope)
        if (!flow?.phases?.length) return { text: `域「${scope}」还没有流程文件。\n· 自动反推：先打几步（turn_card / rules_run 会自动录制），再 flow_learn\n· 手写：flow_save{flow:{entry, phases:[{id, when, legal, needReads}]}}` }
        const lines = [flowText(flow)]
        if (flow.transitions?.length) lines.push('', `阶段转移：${flow.transitions.map((t) => `${t.from}→${t.to}×${t.count}`).join('；')}`)
        const wantsFacts = Boolean(args?.facts || args?.factsPath || args?.useCoachFacts)
        const facts = wantsFacts ? resolveFacts(args) : null
        if (facts) {
          const hit = detectPhase(flow, facts)
          lines.push('', `当前判定：${hit.unknown ? `未识别（${hit.reason}）` : `${hit.phase}｜置信 ${hit.confidence}｜合法动作 ${hit.legal.join('/') || '未记录'}`}`)
          const warnings = softWarnings({ card: hit, facts })
          if (warnings.length) lines.push(...renderWarnings(warnings))
        } else {
          lines.push('', `样本：${store.sessionCount(scope)} 步已录制（sessions.jsonl）`)
        }
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'flow_learn',
      description:
        '从已录制的会话里自动反推流程状态机（用户选的方案：从打的一局里学）：按「场景 + 字段结构」聚类成阶段，用稳定文本和字段有无当判别条件，并记录每阶段的合法动作；写入 flow.json（草稿）并给出覆盖率自检。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          limit: { type: 'number', description: '最多用最近多少步，默认 400' },
          minSupport: { type: 'number', description: '一个阶段至少要有几步支撑，默认 2' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const limit = Math.max(10, Number(args?.limit ?? 400))
        const rows = store.sessions(scope, limit)
        const learned = learnFlow(rows, { scope, minSupport: args?.minSupport })
        if (!learned.ok) return { text: learned.reason }
        const existing = store.flow(scope)
        const target = existing && existing.draft === false ? 'learned' : 'main'
        if (target === 'main') store.saveFlow(scope, learned.flow)
        else store.saveLearnedFlow(scope, learned.flow)
        const r = learned.report
        const lines = [
          `已从 ${r.steps} 步里反推出 ${r.kept} 个阶段（聚类 ${r.clusters} 个${r.dropped.length ? `，丢弃样本不足 ${r.dropped.length} 个` : ''}）`,
          `写入：${target === 'main' ? store.file(scope, 'flow.json') : store.file(scope, 'flow.learned.json')}（草稿）`,
          '',
          flowText(learned.flow),
          '',
          `覆盖率自检：${r.coverage}% 的步骤能被自己的流程认出（应为 100%；低于 90% 说明判别条件冲突）${r.ambiguous ? `｜其中 ${r.ambiguous} 步多阶段同时成立` : ''}`,
        ]
        if (r.transitions.length) lines.push('', `阶段转移：${r.transitions.map((t) => `${t.from}→${t.to}×${t.count}`).join('；')}`)
        const noLegal = learned.flow.phases.filter((p) => !p.legal.length)
        if (noLegal.length) lines.push('', `⚠ ${noLegal.map((p) => p.id).join('、')} 没录到动作：带 action 调 turn_card，或 rules_run 打几步再来学`)
        return { text: lines.join('\n') }
      },
    })

    tools.register({
      name: 'flow_save',
      description: '手写/修改流程状态机并固定下来（draft:false，之后 flow_learn 不会再覆盖它，只会写 flow.learned.json）。phases 每项：{id, label?, when, legal, needReads?}。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string' },
          flow: { type: 'object', additionalProperties: true, description: '流程对象：{entry, phases:[{id, label, when, legal, needReads}]}' },
          merge: { type: 'boolean', description: 'true = 按 id 合并进现有流程（默认整份替换）' },
        },
        required: ['flow'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const scope = String(args?.scope ?? cfg.scope)
        const incoming = args?.flow && typeof args.flow === 'object' ? args.flow : null
        if (!incoming || !Array.isArray(incoming.phases) || !incoming.phases.length) return { text: 'flow.phases 必须是非空数组' }
        const errors = []
        const phases = []
        for (const [i, phase] of incoming.phases.entries()) {
          if (!phase || typeof phase !== 'object') {
            errors.push(`第 ${i + 1} 个阶段不是对象`)
            continue
          }
          const id = String(phase.id ?? '').trim()
          if (!id) errors.push(`第 ${i + 1} 个阶段缺 id`)
          if (!phase.when || typeof phase.when !== 'object') errors.push(`${id || `第 ${i + 1} 个`} 缺 when 条件`)
          if (phase.legal !== undefined && !Array.isArray(phase.legal)) errors.push(`${id} 的 legal 必须是数组`)
          if (errors.length) continue
          phases.push({
            id,
            label: phase.label ? String(phase.label).slice(0, 40) : '',
            when: phase.when,
            legal: (phase.legal ?? []).map(String),
            needReads: (phase.needReads ?? []).map(String),
            support: Number(phase.support ?? 0),
            confidence: Number(phase.confidence ?? 0.9),
            discriminators: Number(phase.discriminators ?? 0),
          })
        }
        if (errors.length) return { text: `流程未写入，校验没通过：\n${errors.map((e) => `- ${e}`).join('\n')}` }
        const existing = store.flow(scope)
        let merged = phases
        if (args?.merge && existing?.phases?.length) {
          const byId = new Map(existing.phases.map((p) => [p.id, p]))
          for (const phase of phases) byId.set(phase.id, phase)
          merged = [...byId.values()]
        }
        const flow = {
          version: 1,
          scope: safeScope(scope),
          draft: false,
          learnedAt: new Date().toISOString(),
          source: incoming.source ?? { manual: true },
          entry: String(incoming.entry ?? merged[0].id),
          phases: merged,
          transitions: incoming.transitions ?? existing?.transitions ?? [],
        }
        store.saveFlow(scope, flow)
        return { text: `已写入流程（${merged.length} 个阶段，draft:false）→ ${store.file(scope, 'flow.json')}\n\n${flowText(flow)}` }
      },
    })
  })

  // 面板接口
  ctx.inject(['webServer'], (serverCtx) => {
    const sendJson = (res, status, payload) => {
      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
      res.end(JSON.stringify(payload))
    }
    const readBody = (req) =>
      new Promise((resolve, reject) => {
        const chunks = []
        let size = 0
        req.on('data', (chunk) => {
          size += chunk.length
          if (size > 2_000_000) {
            reject(new Error('请求体过大'))
            req.destroy()
            return
          }
          chunks.push(chunk)
        })
        req.on('end', () => {
          const raw = Buffer.concat(chunks).toString('utf8').trim()
          if (!raw) return resolve({})
          try {
            resolve(JSON.parse(raw))
          } catch {
            reject(new Error('请求体不是合法 JSON'))
          }
        })
        req.on('error', reject)
      })

    const handle = async (req, res) => {
      const url = new URL(req.url ?? '/', 'http://127.0.0.1')
      const path = url.pathname.replace(/^\/rule-workbench/, '').replace(/\/+$/, '') || '/'
      try {
        if (req.method === 'GET' && (path === '/api/list' || path === '/api/list/')) {
          const scope = String(url.searchParams.get('scope') ?? cfg.scope)
          return sendJson(res, 200, { ok: true, ...listPayload(scope) })
        }
        if (req.method === 'GET' && path === '/api/stats') {
          const scope = String(url.searchParams.get('scope') ?? cfg.scope)
          return sendJson(res, 200, {
            ok: true,
            stats: store.stats(scope),
            unknown: store.unknowns(scope, Number(url.searchParams.get('unknown') ?? 5)),
          })
        }
        if (req.method === 'GET' && path === '/api/health') {
          return sendJson(res, 200, { ok: true, root: store.root, scope: cfg.scope })
        }
        if (req.method === 'POST' && path === '/api/save') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const incoming = Array.isArray(body.rules) ? body.rules : [body.rules]
          const normalized = []
          const errors = []
          for (const [i, raw] of incoming.entries()) {
            const { rule, error } = normalizeRule(raw)
            if (error) errors.push(`第 ${i + 1} 条：${error}`)
            else normalized.push(rule)
          }
          if (errors.length) return sendJson(res, 400, { ok: false, error: errors.join('；') })
          const existing = store.rules(scope)
          const byId = new Map(existing.map((rule) => [rule.id, rule]))
          for (const rule of normalized) byId.set(rule.id, rule)
          store.saveRules(scope, [...byId.values()])
          return sendJson(res, 200, { ok: true, ...listPayload(scope) })
        }
        if (req.method === 'POST' && path === '/api/delete') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const id = String(body.id ?? '')
          store.saveRules(
            scope,
            store.rules(scope).filter((rule) => rule.id !== id),
          )
          return sendJson(res, 200, { ok: true, ...listPayload(scope) })
        }
        if (req.method === 'POST' && path === '/api/toggle') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const rules = store.rules(scope)
          const target = rules.find((rule) => rule.id === String(body.id ?? ''))
          if (!target) return sendJson(res, 404, { ok: false, error: '没有这条规则' })
          target.enabled = Boolean(body.enabled)
          store.saveRules(scope, rules)
          return sendJson(res, 200, { ok: true, ...listPayload(scope) })
        }
        if (req.method === 'POST' && path === '/api/run') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const facts = body.facts && typeof body.facts === 'object' ? body.facts : {}
          const result = runRules(store.rules(scope), facts, { scope })
          if (body.record !== false) {
            store.recordRun(scope, result)
            if (result.ok) store.recordDecision(scope, result)
            else store.appendUnknown(scope, { at: new Date().toISOString(), scope, facts, tried: result.tried })
          }
          return sendJson(res, 200, { ok: true, result })
        }
        if (req.method === 'POST' && path === '/api/outcome') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          try {
            const row = store.closeOutcome(scope, body.id ?? 'latest', String(body.outcome ?? ''), body.note ?? '')
            return sendJson(res, 200, { ok: true, row, ...listPayload(scope) })
          } catch (error) {
            return sendJson(res, 400, { ok: false, error: String(error?.message ?? error) })
          }
        }
        if (req.method === 'GET' && path === '/api/flow') {
          const scope = String(url.searchParams.get('scope') ?? cfg.scope)
          const flow = flowOf(scope)
          return sendJson(res, 200, { ok: true, flow, steps: store.sessionCount(scope), file: store.file(scope, 'flow.json') })
        }
        if (req.method === 'POST' && path === '/api/flow/learn') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const learned = learnFlow(store.sessions(scope, Math.max(10, Number(body.limit ?? 400))), { scope, minSupport: body.minSupport })
          if (!learned.ok) return sendJson(res, 400, { ok: false, error: learned.reason })
          const existing = store.flow(scope)
          // 手写流程（draft:false）不被自动学习覆盖
          if (existing && existing.draft === false) store.saveLearnedFlow(scope, learned.flow)
          else store.saveFlow(scope, learned.flow)
          return sendJson(res, 200, { ok: true, flow: learned.flow, report: learned.report, overridden: Boolean(existing && existing.draft === false) })
        }
        if (req.method === 'POST' && path === '/api/card') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const facts = body.facts && typeof body.facts === 'object' ? body.facts : {}
          const flow = flowOf(scope)
          const hit = flow ? detectPhase(flow, facts) : detectPhase(null, facts)
          const warnings = softWarnings({ card: hit, facts, action: body.action })
          recordStep(scope, facts, { kind: 'card', phase: hit.phase ?? '', action: body.action ?? null, note: body.note ?? '' })
          return sendJson(res, 200, { ok: true, card: hit, warnings, steps: store.sessionCount(scope) })
        }
        return sendJson(res, 404, { ok: false, error: 'not found' })
      } catch (error) {
        logger?.warn?.(`[rule-workbench] 接口失败 ${path}: ${String(error?.message ?? error)}`)
        return sendJson(res, 500, { ok: false, error: String(error?.message ?? error) })
      }
    }

    serverCtx.webServer.register({ kind: 'prefix', path: apiRoot, handler: handle })
  })

  ctx.inject(['systemPrompt'], (promptCtx) => {
    promptCtx.systemPrompt.section({
      name: 'rule-workbench',
      order: 890,
      text: [
        '## 规则工作台（客观决策层）',
        '当任务有明确状态、可重复决策（回合制游戏、桌面流程、有状态的自动化）时，**先写规则再动手**：',
        '1. `rules_doc` 读 DSL；`rules_list` 看当前域已有哪些规则（避免重复）。',
        '2. `rules_save` 写入规则（必须带 why；局面有明确预期就写 `expect`）；`rules_run` 拿一帧事实立刻验证：拿不到明确决策就先修条件，不要猜。',
        '3. 决策只认引擎返回的 `decision`，理由只能引用 `trace`；`alternatives` 是同时成立的备选方案，向用户说明时要把「为什么没选它」一起讲清楚。',
        '4. 决策执行后**必须回填结果**：`rules_outcome{outcome:"win|loss|neutral", note}`（省略 id 即最近一条）。没回填的决策等于没打，规则不会进步。',
        '5. 引擎返回 unknown 时用 `rules_stats` 看未覆盖回合，补规则后 `rules_replay` 回放历史帧确认没打破老局面；`rules_results` 看每条规则的真实胜率。',
        '6. 规则域用 `scope` 区分（例如游戏名），互不干扰。',
        '',
        '## 流程状态机（防「忘记流程」）',
        '- 回合制/有界面流程的任务：每步先 `turn_card{facts}` 拿卡片（阶段 + 合法动作 + 缺读的字段 + 软提醒），不要凭记忆推流程；',
        '- 没有流程文件时：`flow_learn` 从已录步骤自动反推（turn_card 与 rules_run 会自动录进 sessions.jsonl），学完 `flow_get{facts}` 复核；手写用 `flow_save`；',
        '- 软提醒不阻断：动作不在合法列表、缺字段、事实过期都只记在 `warnings` 里——看到就复核画面，但不要因为提醒就不给决策；',
        '- 阶段判错时先补判别条件（flow_save 改 when）或补读字段，别改结论。',
      ].join('\n'),
    })
  })
}
