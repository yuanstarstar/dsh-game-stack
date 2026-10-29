/**
 * @local/dsh-game-guard —— 通用防幻觉闸门（游戏/桌面流程都可用，不绑定任何游戏）
 *
 * 解决的问题：模型「凭记忆」出招——数字在屏幕上，但它是从记忆里取的。
 * 做法：把「记忆」这个输入通道整个堵掉，只剩四条硬件闸门：
 *
 *   ① 读数契约：facts 只许写屏幕上看得见的原始值；出现 canKill/best/should 这类结论字段 → 直接拒。
 *   ② 计算层  ：派生量（够不够杀/挡不挡得住）由表达式引擎算，模型不许心算。
 *   ③ 新鲜度  ：超时（默认 5s）的帧一律拒，不允许拿上一帧冒充这一帧。
 *   ④ 对账    ：决策时写下预期变化，结算后与实盘比；不一致 = 读错，一致 = 判断对。
 *               把「读错」和「打输」分开记，才知道该改读数还是改策略。
 *
 * 工具：guard_doc / guard_schema / guard_calc / guard_check / guard_reconcile / guard_stats
 * 面板：侧边栏「防幻觉闸门」（契约 / 派生量 / 拒绝记录 / 对账）
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'game-guard'
export const inject = ['tools', 'systemPrompt', 'webServer']

const MAX_LEDGER_LINES = 1200
const MAX_STRING = 4000

const DEFAULT_FORBIDDEN = [
  'can[A-Z_].*',
  'is[A-Z_].*',
  'best.*',
  'should.*',
  'recommend.*',
  'decision',
  'plan',
  'expect',
  'outcome',
  'killable',
  'winnable',
  'choice',
  'strategy',
]

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string' } },
    required: ['text'],
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

// ------------------------------------------------------------------ 小工具

const nowMs = () => Date.now()

function rootDir() {
  const home = process.env.DSH_HOME || homedir()
  return join(home, '.dsh', 'game-guard')
}

function safeScope(scope) {
  const s = String(scope ?? 'default').trim() || 'default'
  return s.replace(/[\\/:*?"<>|]/g, '_').slice(0, 80)
}

function kindOf(value) {
  if (Array.isArray(value)) return 'array'
  if (value === null) return 'null'
  return typeof value
}

function getPath(obj, path) {
  const parts = String(path).split('.').filter(Boolean)
  let cur = obj
  const walked = []
  for (const part of parts) {
    walked.push(part)
    if (cur === null || cur === undefined || typeof cur !== 'object') {
      return { found: false, value: undefined, at: walked.join('.') }
    }
    if (!(part in cur)) return { found: false, value: undefined, at: walked.join('.') }
    cur = cur[part]
  }
  return { found: true, value: cur, at: parts.join('.') }
}

function setPath(obj, path, value) {
  const parts = String(path).split('.').filter(Boolean)
  let cur = obj
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i]
    if (typeof cur[part] !== 'object' || cur[part] === null) cur[part] = {}
    cur = cur[part]
  }
  cur[parts[parts.length - 1]] = value
  return obj
}

function num(value, label) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'boolean') return value ? 1 : 0
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) return Number(value)
  throw new Error(`${label} 不是数字：${JSON.stringify(value)}`)
}

const truthy = (value) => (value === 0 || value === false || value === '' || value === null || value === undefined ? 0 : 1)

/** 预期：支持 { path: delta } 与 { changes:[{path,delta,tol}] } 两种写法。 */
function normalizeExpect(input) {
  const out = []
  if (!input || typeof input !== 'object') return out
  if (Array.isArray(input.changes)) {
    for (const item of input.changes) {
      if (!item || typeof item.path !== 'string' || !item.path.trim()) continue
      out.push({
        path: item.path.trim(),
        delta: Number(item.delta ?? 0),
        tol: Number.isFinite(Number(item.tol)) ? Math.abs(Number(item.tol)) : 0,
      })
    }
    return out
  }
  for (const [path, delta] of Object.entries(input)) {
    const value = Number(delta)
    if (Number.isFinite(value)) out.push({ path, delta: value, tol: 0 })
  }
  return out
}

// ------------------------------------------------------------- 表达式引擎

const FUNCS = {
  min: (...args) => Math.min(...args.map((v, i) => num(v, `min 第 ${i + 1} 个参数`))),
  max: (...args) => Math.max(...args.map((v, i) => num(v, `max 第 ${i + 1} 个参数`))),
  abs: (v) => Math.abs(num(v, 'abs 参数')),
  round: (v) => Math.round(num(v, 'round 参数')),
  floor: (v) => Math.floor(num(v, 'floor 参数')),
  ceil: (v) => Math.ceil(num(v, 'ceil 参数')),
  sum: (...args) => args.reduce((acc, v, i) => acc + num(v, `sum 第 ${i + 1} 个参数`), 0),
}

class MissingFact extends Error {
  constructor(path) {
    super(`facts 里没有 ${path}（没读到就是没读到，不许猜）`)
    this.code = 'missing'
    this.path = path
  }
}

function tokenize(src) {
  const out = []
  const s = String(src)
  let i = 0
  while (i < s.length) {
    const c = s[i]
    if (/\s/.test(c)) {
      i += 1
      continue
    }
    const number = /^\d+(?:\.\d+)?/.exec(s.slice(i))
    if (number) {
      out.push({ t: 'num', v: Number(number[0]) })
      i += number[0].length
      continue
    }
    const ident = /^[A-Za-z_][A-Za-z0-9_]*/.exec(s.slice(i))
    if (ident) {
      out.push({ t: 'id', v: ident[0] })
      i += ident[0].length
      continue
    }
    const op = /^(>=|<=|==|!=|&&|\|\||[+\-*/%<>!?:(),.\[\]])/.exec(s.slice(i))
    if (op) {
      out.push({ t: 'op', v: op[0] })
      i += op[0].length
      continue
    }
    throw new Error(`表达式第 ${i} 个字符「${c}」不认识`)
  }
  out.push({ t: 'eof', v: '' })
  return out
}

/** 编译表达式；返回 (facts) => number，缺字段抛 MissingFact。 */
function compileExpr(src) {
  const tokens = tokenize(src)
  let pos = 0
  const peek = () => tokens[pos]
  const isOp = (v) => tokens[pos].t === 'op' && tokens[pos].v === v
  const eat = (v) => {
    if (isOp(v)) {
      pos += 1
      return true
    }
    return false
  }
  const need = (v) => {
    if (!eat(v)) throw new Error(`表达式缺「${v}」（${src}）`)
  }

  function parseExpr() {
    return parseTernary()
  }

  function parseTernary() {
    const cond = parseOr()
    if (eat('?')) {
      const a = parseTernary()
      need(':')
      const b = parseTernary()
      return (facts) => (truthy(cond(facts)) ? a(facts) : b(facts))
    }
    return cond
  }

  function parseOr() {
    let left = parseAnd()
    while (isOp('||')) {
      pos += 1
      const right = parseAnd()
      const L = left
      left = (facts) => (truthy(L(facts)) || truthy(right(facts)) ? 1 : 0)
    }
    return left
  }

  function parseAnd() {
    let left = parseEquality()
    while (isOp('&&')) {
      pos += 1
      const right = parseEquality()
      const L = left
      left = (facts) => (truthy(L(facts)) && truthy(right(facts)) ? 1 : 0)
    }
    return left
  }

  function parseEquality() {
    let left = parseCompare()
    while (isOp('==') || isOp('!=')) {
      const op = tokens[pos].v
      pos += 1
      const right = parseCompare()
      const L = left
      left = (facts) => {
        const a = num(L(facts), src)
        const b = num(right(facts), src)
        return (op === '==' ? a === b : a !== b) ? 1 : 0
      }
    }
    return left
  }

  function parseCompare() {
    let left = parseAdditive()
    while (isOp('>') || isOp('>=') || isOp('<') || isOp('<=')) {
      const op = tokens[pos].v
      pos += 1
      const right = parseAdditive()
      const L = left
      left = (facts) => {
        const a = num(L(facts), src)
        const b = num(right(facts), src)
        if (op === '>') return a > b ? 1 : 0
        if (op === '>=') return a >= b ? 1 : 0
        if (op === '<') return a < b ? 1 : 0
        return a <= b ? 1 : 0
      }
    }
    return left
  }

  function parseAdditive() {
    let left = parseMultiplicative()
    while (isOp('+') || isOp('-')) {
      const op = tokens[pos].v
      pos += 1
      const right = parseMultiplicative()
      const L = left
      left = (facts) => (op === '+' ? num(L(facts), src) + num(right(facts), src) : num(L(facts), src) - num(right(facts), src))
    }
    return left
  }

  function parseMultiplicative() {
    let left = parseUnary()
    while (isOp('*') || isOp('/') || isOp('%')) {
      const op = tokens[pos].v
      pos += 1
      const right = parseUnary()
      const L = left
      left = (facts) => {
        const a = num(L(facts), src)
        const b = num(right(facts), src)
        if (op === '*') return a * b
        if (b === 0) throw new Error(`表达式除以零：${src}`)
        return op === '/' ? a / b : a % b
      }
    }
    return left
  }

  function parseUnary() {
    if (eat('-')) {
      const inner = parseUnary()
      return (facts) => -num(inner(facts), src)
    }
    if (eat('!')) {
      const inner = parseUnary()
      return (facts) => (truthy(inner(facts)) ? 0 : 1)
    }
    return parsePrimary()
  }

  function parsePrimary() {
    const token = peek()
    if (token.t === 'num') {
      pos += 1
      const v = token.v
      return () => v
    }
    if (isOp('(')) {
      pos += 1
      const inner = parseExpr()
      need(')')
      return inner
    }
    if (token.t === 'id') {
      pos += 1
      const label = token.v
      if (isOp('(')) {
        const fn = FUNCS[label.toLowerCase()]
        if (!fn) throw new Error(`表达式里没有函数 ${label}（可用：${Object.keys(FUNCS).join(' / ')}）`)
        pos += 1
        const args = []
        if (!isOp(')')) {
          args.push(parseExpr())
          while (eat(',')) args.push(parseExpr())
        }
        need(')')
        return (facts) => fn(...args.map((arg) => arg(facts)))
      }
      const parts = [label]
      while (isOp('.') || isOp('[')) {
        if (eat('.')) {
          const next = peek()
          if (next.t !== 'id' && next.t !== 'num') throw new Error(`路径 ${parts.join('.')} 后面缺字段名`)
          pos += 1
          parts.push(String(next.v))
        } else {
          eat('[')
          const next = peek()
          if (next.t !== 'num' && next.t !== 'id') throw new Error(`路径 ${parts.join('.')} 的 [ ] 里缺索引`)
          pos += 1
          parts.push(String(next.v))
          need(']')
        }
      }
      const path = parts.join('.')
      return (facts) => {
        const hit = getPath(facts, path)
        if (!hit.found) throw new MissingFact(path)
        return hit.value
      }
    }
    throw new Error(`表达式认不出这个位置：${JSON.stringify(token.v)}（${src}）`)
  }

  const fn = parseExpr()
  if (peek().t !== 'eof') throw new Error(`表达式结尾多了东西：${peek().v}（${src}）`)
  return fn
}

// ---------------------------------------------------------------- 契约存储

class Store {
  constructor(logger) {
    this.logger = logger
    this.root = rootDir()
    mkdirSync(this.root, { recursive: true })
  }

  dir(scope) {
    const d = join(this.root, safeScope(scope))
    mkdirSync(d, { recursive: true })
    return d
  }

  file(scope, name) {
    return join(this.dir(scope), name)
  }

  readJson(path, fallback) {
    try {
      if (!existsSync(path)) return fallback
      return JSON.parse(readFileSync(path, 'utf8'))
    } catch {
      return fallback
    }
  }

  writeJson(path, payload) {
    const tmp = `${path}.tmp`
    writeFileSync(tmp, JSON.stringify(payload, null, 2), 'utf8')
    renameSync(tmp, path)
  }

  schema(scope) {
    const raw = this.readJson(this.file(scope, 'schema.json'), null)
    return normalizeSchema(raw)
  }

  saveSchema(scope, payload) {
    const schema = normalizeSchema(payload, { strict: true })
    this.writeJson(this.file(scope, 'schema.json'), schema)
    return schema
  }

  stats(scope) {
    return this.readJson(this.file(scope, 'stats.json'), {
      checks: 0,
      refusals: 0,
      byCode: {},
      missing: {},
      reconcile: { match: 0, mismatch: 0 },
      lastAt: '',
    })
  }

  saveStats(scope, stats) {
    this.writeJson(this.file(scope, 'stats.json'), stats)
  }

  appendLedger(scope, entry) {
    const file = this.file(scope, 'ledger.jsonl')
    appendFileSync(file, `${JSON.stringify(entry)}\n`, 'utf8')
    try {
      const text = readFileSync(file, 'utf8')
      const lines = text.split('\n').filter(Boolean)
      if (lines.length > MAX_LEDGER_LINES) {
        writeFileSync(file, `${lines.slice(-MAX_LEDGER_LINES).join('\n')}\n`, 'utf8')
      }
    } catch {
      /* 忽略 */
    }
  }

  ledger(scope, limit = 20) {
    try {
      const file = this.file(scope, 'ledger.jsonl')
      if (!existsSync(file)) return []
      const lines = readFileSync(file, 'utf8').split('\n').filter(Boolean)
      return lines
        .slice(-Math.max(1, Math.min(200, limit)))
        .map((line) => {
          try {
            return JSON.parse(line)
          } catch {
            return null
          }
        })
        .filter(Boolean)
        .reverse()
    } catch {
      return []
    }
  }

  scopes() {
    try {
      return readdirSync(this.root, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name)
    } catch {
      return []
    }
  }
}

function normalizeSchema(raw, opts = {}) {
  const out = {
    scope: String(raw?.scope ?? ''),
    maxAgeMs: Number.isFinite(Number(raw?.maxAgeMs)) ? Math.max(0, Number(raw.maxAgeMs)) : 5000,
    required: [],
    types: {},
    forbidden: Array.isArray(raw?.forbidden) && raw.forbidden.length ? raw.forbidden.map(String) : DEFAULT_FORBIDDEN.slice(),
    derived: [],
    note: String(raw?.note ?? ''),
  }
  const required = Array.isArray(raw?.required) ? raw.required : []
  for (const item of required) {
    if (typeof item === 'string' && item.trim()) {
      out.required.push({ path: item.trim(), kind: 'any', note: '' })
      continue
    }
    if (item && typeof item === 'object' && typeof item.path === 'string' && item.path.trim()) {
      out.required.push({
        path: item.path.trim(),
        kind: ['number', 'string', 'boolean', 'array', 'object', 'any'].includes(item.kind) ? item.kind : 'any',
        note: String(item.note ?? ''),
      })
    }
  }
  if (raw?.types && typeof raw.types === 'object') {
    for (const [path, kind] of Object.entries(raw.types)) {
      if (typeof path === 'string' && ['number', 'string', 'boolean', 'array', 'object'].includes(kind)) out.types[path] = kind
    }
  }
  const derived = Array.isArray(raw?.derived) ? raw.derived : []
  for (const item of derived) {
    if (!item || typeof item !== 'object') continue
    const path = typeof item.path === 'string' ? item.path.trim() : ''
    const expr = typeof item.expr === 'string' ? item.expr.trim() : ''
    if (!path || !expr) {
      if (opts.strict) throw new Error('派生量每项都要有 path 与 expr')
      continue
    }
    let compiled
    try {
      compiled = compileExpr(expr)
    } catch (error) {
      if (opts.strict) throw new Error(`派生量 ${path} 的表达式有语法错误：${String(error?.message ?? error)}`)
      continue
    }
    out.derived.push({ path, expr, note: String(item.note ?? ''), _compiled: compiled })
  }
  return out
}

function plainSchema(schema) {
  return {
    scope: schema.scope,
    maxAgeMs: schema.maxAgeMs,
    required: schema.required,
    types: schema.types,
    forbidden: schema.forbidden,
    derived: schema.derived.map((item) => ({ path: item.path, expr: item.expr, note: item.note })),
    note: schema.note,
  }
}

// ------------------------------------------------------------ 检查与计算

function collectKeys(value, prefix = '', out = []) {
  if (value === null || typeof value !== 'object') return out
  for (const [key, val] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    out.push({ key, path })
    if (val && typeof val === 'object' && !Array.isArray(val)) collectKeys(val, path, out)
  }
  return out
}

function checkFacts(schema, facts, opts = {}) {
  const problems = []
  const derivedPaths = new Set((schema.derived ?? []).map((item) => item.path))
  const f = facts && typeof facts === 'object' && !Array.isArray(facts) ? facts : null
  if (!f) {
    return { problems: [{ code: 'no-facts', severity: 'fatal', message: '没有 facts：先读盘，别凭记忆下结论' }], ageMs: null }
  }
  // ① 结论字段（幻觉的主要入口）
  const patterns = (schema.forbidden?.length ? schema.forbidden : DEFAULT_FORBIDDEN).map((pat) => {
    try {
      return new RegExp(`^${pat}$`)
    } catch {
      return null
    }
  }).filter(Boolean)
  for (const { key, path } of collectKeys(f)) {
    if (path.startsWith('__')) continue
    // 引擎算出来的派生量本身就是结论：这些路径允许存在（那是算出来的，不是编的）
    if (derivedPaths.has(path) || path.startsWith('calc.') || path.startsWith('derived.')) continue
    if (patterns.some((re) => re.test(key))) {
      problems.push({ code: 'conclusion', severity: 'fatal', path, message: `${path} 是结论字段（${key}）——facts 只许写屏幕上看得见的原始值` })
    }
  }
  // ② 必填字段与类型
  for (const item of schema.required) {
    const hit = getPath(f, item.path)
    if (!hit.found || hit.value === null || hit.value === '') {
      problems.push({ code: 'missing', severity: 'fatal', path: item.path, message: `缺 ${item.path}${item.note ? `（${item.note}）` : ''}` })
      continue
    }
    const want = item.kind !== 'any' ? item.kind : schema.types[item.path]
    if (want) {
      const got = kindOf(hit.value)
      if (got !== want) {
        problems.push({ code: 'type', severity: 'fatal', path: item.path, message: `${item.path} 应该是 ${want}，实际是 ${got}` })
      }
    }
  }
  for (const [path, want] of Object.entries(schema.types ?? {})) {
    const hit = getPath(f, path)
    if (!hit.found || hit.value === null || hit.value === '') continue
    const got = kindOf(hit.value)
    if (got !== want) problems.push({ code: 'type', severity: 'fatal', path, message: `${path} 应该是 ${want}，实际是 ${got}` })
  }
  // ③ 新鲜度
  let ageMs = null
  if (Number.isFinite(Number(f.__ageMs))) ageMs = Math.max(0, Number(f.__ageMs))
  else if (typeof f.__savedAt === 'string' && f.__savedAt) {
    const t = Date.parse(f.__savedAt)
    if (Number.isFinite(t)) ageMs = Math.max(0, nowMs() - t)
  }
  if (ageMs === null) {
    problems.push({ code: 'no-age', severity: 'warn', message: '这帧没有 __savedAt/__ageMs：算不出新鲜度，守卫只能放行' })
  } else if (ageMs > schema.maxAgeMs) {
    problems.push({ code: 'stale', severity: 'fatal', message: `这帧已经 ${Math.round(ageMs / 100) / 10}s 了（上限 ${schema.maxAgeMs}ms）：回去重读，不许用旧帧` })
  }
  // ④ 引用必须来自这一帧
  const cites = Array.isArray(opts.cites) ? opts.cites : []
  for (const cite of cites) {
    const path = String(cite ?? '').trim()
    if (!path) continue
    const hit = getPath(f, path)
    if (!hit.found || hit.value === undefined || hit.value === null || hit.value === '') {
      problems.push({ code: 'unread-cite', severity: 'fatal', path, message: `你引用了 ${path}，但这帧里没有读到它——别假装读过` })
    }
  }
  return { problems, ageMs }
}

function verdictOf(problems) {
  return problems.some((p) => p.severity === 'fatal') ? 'refused' : problems.length ? 'warn' : 'ok'
}

function computeDerived(schema, rawFacts) {
  const facts = JSON.parse(JSON.stringify(rawFacts ?? {}))
  const derived = []
  const missing = []
  for (const item of schema.derived) {
    const compiled = item._compiled ?? compileExpr(item.expr)
    try {
      const value = compiled(facts)
      const rounded = Number.isFinite(value) ? Math.round(value * 1000) / 1000 : value
      setPath(facts, item.path, rounded)
      derived.push({ path: item.path, value: rounded, expr: item.expr, note: item.note })
    } catch (error) {
      if (error instanceof MissingFact) missing.push({ path: item.path, expr: item.expr, missing: error.path })
      else missing.push({ path: item.path, expr: item.expr, missing: '', error: String(error?.message ?? error) })
    }
  }
  return { facts, derived, missing }
}

function stampFacts(facts) {
  const out = facts && typeof facts === 'object' && !Array.isArray(facts) ? facts : {}
  out.__derivedAt = new Date().toISOString()
  return out
}

function bumpStats(stats, { problems = [], kind = 'check', verdict = '' } = {}) {
  const next = structuredCloneSafe(stats)
  if (kind === 'reconcile') {
    if (verdict === 'match') next.reconcile.match += 1
    else next.reconcile.mismatch += 1
  } else {
    next.checks += 1
    if (verdict === 'refused') {
      next.refusals += 1
      for (const problem of problems) {
        const code = problem.code || 'unknown'
        next.byCode[code] = (next.byCode[code] ?? 0) + 1
        if (code === 'missing' && problem.path) next.missing[problem.path] = (next.missing[problem.path] ?? 0) + 1
      }
    }
  }
  next.lastAt = new Date().toISOString()
  return next
}

function structuredCloneSafe(value) {
  try {
    return structuredClone(value)
  } catch {
    return JSON.parse(JSON.stringify(value))
  }
}

// ------------------------------------------------------------------ 文案

const DSL_DOC = [
  '# 防幻觉闸门（game-guard）速查',
  '',
  '一句话：**facts 只写看得见的原始值，结论交给引擎算，旧帧一律拒。**',
  '',
  '## 工具',
  '- `guard_schema{scope, action:"get"|"save", schema:{...}}` —— 每个游戏/流程一份契约',
  '- `guard_calc{scope, raw}` —— 原始读数 → facts（自动补齐派生量，缺字段列出来）',
  '- `guard_check{scope, facts, cites:[...]}` —— 出招前的闸门：verdict=ok / warn / refused',
  '- `guard_reconcile{scope, id?, expect:{...}, observed}` —— 结算后对账：一致=判断对，不一致=读错',
  '- `guard_stats{scope}` —— 拒绝原因排行、缺字段排行、对账命中率',
  '',
  '## schema 结构',
  '```json',
  '{',
  '  "maxAgeMs": 5000,',
  '  "required": [{ "path": "self.hp", "kind": "number", "note": "左下血条" }],',
  '  "forbidden": ["can.*", "best.*", "decision"],',
  '  "derived": [{ "path": "calc.killable", "expr": "calc.damageBudget >= target.hp ? 1 : 0" }]',
  '}',
  '```',
  '- `required` 缺一个就 **refused**（没有默认值，不许拿上一帧补）',
  '- `forbidden` 命中任一字段名就 refused（结论型字段 = 幻觉入口）',
  '- `derived` 按顺序算，后面可以引用前面算出来的派生量',
  '',
  '## 表达式语言',
  '- 数字、路径（`self.hp`、`pages.0.cost`）、`+ - * / %`、比较 `> >= < <= == !=`、`&& || !`、`? :`',
  '- 函数：`min max abs round floor ceil sum`',
  '- 路径不存在 → 该派生量记为缺失（`missing`），不会拿 0 冒充',
  '',
  '## 四道闸门与实战五步',
  '1. 读：屏幕上这一帧的原始数字（含 `__savedAt`）',
  '2. 算：`guard_calc` → 派生量由代码算，模型不心算',
  '3. 决：决策（规则工作台 `rules_run`；闸门先 `guard_check` 过一遍）',
  '4. 执：按决策出招',
  '5. 对：`guard_reconcile{expect, observed}` —— 不一致先怀疑读数，再怀疑规则',
  '',
  '**停止条件**：连续两回合 refused，说明这一屏读不出来——停手，不要靠想象往下打。',
].join('\n')

const PROMPT_TEXT = [
  '## 防幻觉闸门（game-guard）',
  '任何「凭记忆出招」都是幻觉。有 game-guard 工具时按这个顺序走：',
  '1. 先 `guard_check`（或先用 `guard_calc` 算派生量再 check）；返回 refused 就**回去重读屏幕**，不要解释、不要补值；',
  '2. 决策只能引用这一帧里真实存在的字段；`cites` 里写了却没读到的字段会被拦；',
  '3. 出招后把预期变化写进 `guard_reconcile`，结算后比对：不一致 = 你读错了，不是运气不好。',
  '4. 缺字段不许用「上次是多少」填，`__ageMs` 超时（默认 5s）的帧一律作废。',
].join('\n')

// ------------------------------------------------------------------ 插件

export function apply(ctx, config) {
  const cfg = { ...(config ?? {}) }
  if (!cfg.scope) cfg.scope = 'default'
  const logger = ctx.logger?.('game-guard') ?? ctx.logger
  const store = new Store(logger)
  const apiRoot = '/game-guard'

  /** 闸门：算上派生量后再判一遍（派生量也是 facts 的一部分）。 */
  const runCalc = (scope, raw, extra = {}) => {
    const schema = store.schema(scope)
    const { facts, derived, missing } = computeDerived(schema, raw)
    const stamped = stampFacts(facts)
    const check = checkFacts(schema, stamped, extra)
    const problems = [...check.problems]
    for (const item of missing) {
      problems.push({
        code: 'derived-missing',
        severity: 'warn',
        path: item.path,
        message: `派生量 ${item.path} 算不出来${item.missing ? `（缺 ${item.missing}）` : item.error ? `（${item.error}）` : ''}`,
      })
    }
    const verdict = verdictOf(problems)
    return { schema, facts: stamped, derived, missing, problems, verdict, ageMs: check.ageMs }
  }

  const record = (scope, payload, stats) => {
    store.appendLedger(scope, { at: new Date().toISOString(), scope: safeScope(scope), ...payload })
    store.saveStats(scope, stats)
  }

  const summary = (scope) => {
    const schema = store.schema(scope)
    const stats = store.stats(scope)
    return {
      scope,
      maxAgeMs: schema.maxAgeMs,
      required: schema.required,
      types: schema.types,
      derived: schema.derived.map((item) => ({ path: item.path, expr: item.expr, note: item.note })),
      forbidden: schema.forbidden,
      note: schema.note,
      stats,
      ledger: store.ledger(scope, 12),
      scopes: store.scopes(),
    }
  }

  ctx.inject(['tools'], (toolsCtx) => {
    const tools = toolsCtx.tools

    tools.register({
      name: 'guard_doc',
      description: '防幻觉闸门速查：四道闸门（读数契约/派生量/新鲜度/对账）、schema 结构、表达式语言、实战五步。动手前先读。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: TEXT_OUTPUT,
      async execute() {
        return { text: DSL_DOC }
      },
    })

    tools.register({
      name: 'guard_schema',
      description: '查看或保存某个范围（scope，例如游戏名）的读数契约：必填字段/类型/结论型禁用词/派生量表达式/帧龄上限。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string', description: '契约范围；省略用配置里的默认域' },
          action: { type: 'string', enum: ['get', 'save'], description: 'get 查看（默认）；save 保存' },
          schema: { type: 'object', description: 'action=save 时给：{maxAgeMs?, required?, types?, forbidden?, derived?, note?}' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const scope = String(a.scope ?? cfg.scope)
        if (a.action === 'save') {
          if (!a.schema || typeof a.schema !== 'object') throw new Error('action=save 需要 schema 对象')
          const saved = store.saveSchema(scope, a.schema)
          return {
            text: [
              `已保存契约「${scope}」`,
              `- 必填 ${saved.required.length} 项｜派生量 ${saved.derived.length} 条｜禁用词 ${saved.forbidden.length} 个｜帧龄上限 ${saved.maxAgeMs}ms`,
              saved.required.length ? `\n必填：${saved.required.map((item) => item.path).join('、')}` : '',
              saved.derived.length ? `\n派生：\n${saved.derived.map((item) => `  - ${item.path} = ${item.expr}`).join('\n')}` : '',
            ].filter(Boolean).join('\n'),
          }
        }
        const schema = store.schema(scope)
        const stats = store.stats(scope)
        return {
          text: [
            `# 契约「${scope}」`,
            `帧龄上限 ${schema.maxAgeMs}ms${schema.note ? `｜${schema.note}` : ''}`,
            '',
            schema.required.length ? `必填：\n${schema.required.map((item) => `  - ${item.path}（${item.kind}）${item.note ? ` ${item.note}` : ''}`).join('\n')}` : '必填：（空——先 guard_schema{save} 定义）',
            '',
            schema.derived.length ? `派生量：\n${schema.derived.map((item) => `  - ${item.path} = ${item.expr}${item.note ? `  // ${item.note}` : ''}`).join('\n')}` : '派生量：（空）',
            '',
            `禁用词：${schema.forbidden.join(' / ')}`,
            `统计：检查 ${stats.checks} 次，拒 ${stats.refusals} 次；对账 一致 ${stats.reconcile.match} / 不一致 ${stats.reconcile.mismatch}`,
            `已知范围：${store.scopes().join('、') || '（无）'}`,
          ].join('\n'),
        }
      },
    })

    tools.register({
      name: 'guard_calc',
      description: '把屏幕原始读数变成 facts：校验必填/禁用词/新鲜度，并按契约表达式算出派生量（够不够杀、挡不挡得住等）。缺的字段会列出来，它自己补。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string', description: '契约范围' },
          raw: { type: 'object', description: '原始读数：只许屏幕看得见的数字/布尔/枚举，附 __savedAt 更佳' },
        },
        required: ['raw'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const scope = String(a.scope ?? cfg.scope)
        if (!a.raw || typeof a.raw !== 'object') throw new Error('raw 需要一个对象（屏幕读数）')
        const result = runCalc(scope, a.raw)
        const stats = bumpStats(store.stats(scope), { problems: result.problems, kind: 'calc', verdict: result.verdict })
        record(scope, { kind: 'calc', verdict: result.verdict, problems: result.problems, missing: result.missing }, stats)
        return {
          text: [
            result.verdict === 'refused' ? '⛔ 拒止：这一帧还不能用（先回去读盘）' : result.verdict === 'warn' ? '⚠ 有提醒，但可以继续' : '✅ facts 可用',
            '',
            '派生量：',
            result.derived.length ? result.derived.map((item) => `  - ${item.path} = ${item.value}${item.note ? `  // ${item.note}` : ''}`).join('\n') : '  （契约里没定义）',
            result.missing.length ? `\n算不出来：\n${result.missing.map((item) => `  - ${item.path}（缺 ${item.missing || item.error}）`).join('\n')}` : '',
            result.problems.length ? `\n问题：\n${result.problems.map((item) => `  - [${item.code}] ${item.message}`).join('\n')}` : '',
            '',
            'facts（下一步可喂 rules_run 的 facts）：',
            '```json',
            JSON.stringify(result.facts, null, 2).slice(0, MAX_STRING),
            '```',
          ].filter(Boolean).join('\n'),
        }
      },
    })

    tools.register({
      name: 'guard_check',
      description: '出招前的闸门：给定 facts（可带 cites 引用列表），返回 ok/warn/refused 与具体问题。refused 时必须回去重读，不许解释或补值。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string', description: '契约范围' },
          facts: { type: 'object', description: '这一帧的 facts' },
          cites: { type: 'array', items: { type: 'string' }, description: '你要引用的字段路径；其中一个没读到就拒' },
        },
        required: ['facts'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const scope = String(a.scope ?? cfg.scope)
        const schema = store.schema(scope)
        const { problems, ageMs } = checkFacts(schema, a.facts, { cites: a.cites })
        const verdict = verdictOf(problems)
        const stats = bumpStats(store.stats(scope), { problems, kind: 'check', verdict })
        record(scope, { kind: 'check', verdict, problems }, stats)
        return {
          text: [
            verdict === 'ok' ? '✅ 放行' : verdict === 'warn' ? '⚠ 放行（有提醒）' : '⛔ 拒止：回去重读屏幕',
            ageMs === null ? '帧龄：未知' : `帧龄：${Math.round(ageMs)}ms（上限 ${schema.maxAgeMs}ms）`,
            problems.length ? `\n问题：\n${problems.map((item) => `  - [${item.code}] ${item.message}`).join('\n')}` : '',
            verdict === 'refused' ? '\n按规矩：**现在不许出招**。重读画面 → guard_calc → 再 check。连续两回合拒止就停手，不要靠想象往下打。' : '',
          ].filter(Boolean).join('\n'),
        }
      },
    })

    tools.register({
      name: 'guard_reconcile',
      description: '结算后对账：把出招时写下的预期变化（expect）与实盘 observed 比，一致=判断对，不一致=你读错了。分别计数，避免把「读错」记成「运气差」。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string', description: '契约范围' },
          expect: {
            type: 'object',
            description: '预期：{ "target.hp": -12, "target.stagger": -30 } 或 { changes:[{path, delta, tol}] }',
          },
          observed: { type: 'object', description: '结算后实盘读数（同一套路径）' },
          note: { type: 'string', description: '一句话备注' },
        },
        required: ['expect', 'observed'],
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const scope = String(a.scope ?? cfg.scope)
        const expected = normalizeExpect(a.expect)
        const observed = a.observed && typeof a.observed === 'object' ? a.observed : {}
        const diffs = []
        for (const item of expected) {
          const hit = getPath(observed, item.path)
          const plain = Object.prototype.hasOwnProperty.call(observed, item.path) ? observed[item.path] : undefined
          const value = hit.found ? hit.value : plain
          if (value === undefined || value === null) {
            diffs.push({ path: item.path, expect: item.delta, observed: null, ok: false, reason: '实盘没读到这个字段' })
            continue
          }
          const delta = typeof value === 'number' ? value : Number(value)
          if (!Number.isFinite(delta)) {
            diffs.push({ path: item.path, expect: item.delta, observed: value, ok: false, reason: '实盘不是数字' })
            continue
          }
          const ok = Math.abs(delta - item.delta) <= item.tol
          diffs.push({ path: item.path, expect: item.delta, observed: delta, ok, reason: ok ? '' : `差 ${Math.round((delta - item.delta) * 100) / 100}` })
        }
        const verdict = diffs.length && diffs.every((d) => d.ok) ? 'match' : 'mismatch'
        const stats = bumpStats(store.stats(scope), { kind: 'reconcile', verdict })
        record(scope, { kind: 'reconcile', verdict, expect: expected, diffs, note: String(a.note ?? '') }, stats)
        return {
          text: [
            verdict === 'match' ? '✅ 对账一致：预期兑现，这次读数是准的' : '❗ 对账不一致：先怀疑自己**读错**（不是运气不好）',
            '',
            diffs.length ? diffs.map((d) => `  - ${d.path}：预期 ${d.expect >= 0 ? '+' : ''}${d.expect}，实盘 ${d.observed === null ? '（没读到）' : d.observed}${d.ok ? '' : ` ← ${d.reason}`}`).join('\n') : '  （预期是空的）',
            '',
            `统计：一致 ${stats.reconcile.match} 次，不一致 ${stats.reconcile.mismatch} 次`,
            verdict === 'mismatch' ? '\n下一步：回去重读同一处 → 若读数与刚才不同，就是刚才看错了；连对两次不一致才考虑规则本身有问题。' : '',
          ].filter(Boolean).join('\n'),
        }
      },
    })

    tools.register({
      name: 'guard_stats',
      description: '看闸门的运行统计：检查/拒止次数、拒绝原因排行、最容易缺的字段、对账一致率、最近拒绝记录。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          scope: { type: 'string', description: '契约范围' },
          ledger: { type: 'number', description: '最近记录条数，默认 8' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const scope = String(a.scope ?? cfg.scope)
        const stats = store.stats(scope)
        const ledger = store.ledger(scope, Number.isFinite(Number(a.ledger)) ? Number(a.ledger) : 8)
        const rank = (obj) => Object.entries(obj ?? {}).sort((x, y) => y[1] - x[1]).slice(0, 8)
        return {
          text: [
            `# 防幻觉闸门统计「${scope}」`,
            `检查 ${stats.checks} 次｜拒止 ${stats.refusals} 次${stats.checks ? `（${Math.round((stats.refusals / stats.checks) * 100)}%）` : ''}`,
            `对账：一致 ${stats.reconcile.match}｜不一致 ${stats.reconcile.mismatch}${stats.reconcile.match + stats.reconcile.mismatch ? `（一致率 ${Math.round((stats.reconcile.match / (stats.reconcile.match + stats.reconcile.mismatch)) * 100)}%）` : ''}`,
            '',
            rank(stats.byCode).length ? `拒绝原因：\n${rank(stats.byCode).map(([code, n]) => `  - ${code}：${n}`).join('\n')}` : '拒绝原因：（还没有）',
            '',
            rank(stats.missing).length ? `最容易缺的字段：\n${rank(stats.missing).map(([path, n]) => `  - ${path}：${n} 次`).join('\n')}` : '最容易缺的字段：（还没有）',
            '',
            ledger.length ? `最近记录：\n${ledger.map((entry) => `  - ${entry.at.slice(11, 19)} ${entry.kind}/${entry.verdict}${entry.problems?.length ? ` ${entry.problems.map((p) => p.code).join(',')}` : ''}${entry.note ? ` // ${entry.note}` : ''}`).join('\n')}` : '最近记录：（无）',
            `\n已知范围：${store.scopes().join('、') || '（无）'}`,
          ].join('\n'),
        }
      },
    })
  })

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
      const path = url.pathname.replace(/^\/game-guard/, '').replace(/\/+$/, '') || '/'
      try {
        if (req.method === 'GET' && (path === '/api/guard' || path === '/api/guard/')) {
          const scope = String(url.searchParams.get('scope') ?? cfg.scope)
          return sendJson(res, 200, { ok: true, ...summary(scope) })
        }
        if (req.method === 'GET' && path === '/api/health') {
          return sendJson(res, 200, { ok: true, root: store.root, scope: cfg.scope })
        }
        if (req.method === 'POST' && path === '/api/check') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const schema = store.schema(scope)
          const { problems, ageMs } = checkFacts(schema, body.facts, { cites: body.cites })
          const verdict = verdictOf(problems)
          const stats = bumpStats(store.stats(scope), { problems, kind: 'check', verdict })
          record(scope, { kind: 'check', verdict, problems }, stats)
          return sendJson(res, 200, { ok: true, verdict, ageMs, problems })
        }
        if (req.method === 'POST' && path === '/api/schema') {
          const body = await readBody(req)
          const scope = String(body.scope ?? cfg.scope)
          const saved = store.saveSchema(scope, body.schema)
          return sendJson(res, 200, { ok: true, schema: plainSchema(saved) })
        }
        return sendJson(res, 404, { ok: false, error: `未知路径 ${path}`, paths: ['/api/guard', '/api/check', '/api/schema', '/api/health'] })
      } catch (error) {
        return sendJson(res, 400, { ok: false, error: String(error?.message ?? error) })
      }
    }

    serverCtx.webServer.register({ kind: 'prefix', path: apiRoot, handler: handle })
  })

  ctx.inject(['systemPrompt'], (promptCtx) => {
    promptCtx.systemPrompt.section({
      name: 'game-guard',
      order: 906,
      text: PROMPT_TEXT,
    })
  })
}
