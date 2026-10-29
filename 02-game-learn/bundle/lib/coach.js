/**
 * 回合制游戏教练 · Host 半边。
 *
 * 三类能力，全部落在本地 JSON 资料库里：
 *  1. 游戏档案（按 回合制 / 可暂停策略 / 2D实时 / 3D实时 四类归档）；
 *  2. 策略库（研究得到 + 实战沉淀得到），带 L0/L1/L2 分层与胜负计数；
 *  3. 实战回合记录（局面 → 合法动作 → 决策 → 结果 → 教训）。
 *
 * 另外注册一个只读/少量写入的 HTTP 接口，供 Client 面板取数据。
 */
import { appendFile, mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

/** Cordis 插件名。 */
export const name = 'game-coach'

/** 分类与分层的稳定取值。 */
const CATEGORIES = ['turn', 'paused', 'rt2d', 'rt3d']
const CATEGORY_LABEL = {
  turn: '回合制',
  paused: '可暂停策略',
  rt2d: '2D实时',
  rt3d: '3D实时',
}
const TIERS = ['L0', 'L1', 'L2']
const TIER_LABEL = {
  L0: 'L0 稳定套路（几乎总这么走）',
  L1: 'L1 条件化（看局面分支）',
  L2: 'L2 实验性（待验证）',
}

/** 注入宿主时的提示段落。 */
const PROTOCOL = [
  '## 回合制游戏教练（game-coach）',
  '当用户在打回合制或可暂停的游戏、并问「这一步怎么走」时，按下面的循环工作，不要凭记忆编造：',
  '1. 定位游戏：`game_coach_game(action:"list")`，没有档案就先 `register`（写清规则、胜负条件、当前版本/模组）。',
  '2. 先查策略：`game_coach_strategy(action:"search")`。库里没有可用条目时，先研究（推理或资料）再 `save` 成条目：L0=稳定套路，L1=条件化局面判断，L2=实验性。',
  '3. 每回合只推进一步：把可见状态与**合法动作列表**摊开，决策必须返回「唯一动作 ID + 一句理由 + 备选与风险」，动作 ID 必须来自合法列表。',
  '4. 落账：`game_coach_turn(action:"log")` 记录局面与决策；有结果后 `action:"finish"` 写结果与教训；策略用 `rate` 记胜负。',
  '5. 用户没要求就不要替他改存档、不要连走多步；不确定的信息先问。',
].join('\n')

const uid = (prefix) =>
  `${prefix}_${Date.now().toString(36).slice(-5)}${Math.random().toString(36).slice(2, 6)}`

const now = () => new Date().toISOString()

const asArray = (value) =>
  Array.isArray(value) ? value.map((item) => String(item)).filter((item) => item !== '') : []

const clip = (value, max = 400) => {
  const text = String(value ?? '').trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/** 空白资料库。 */
const emptyLibrary = () => ({ version: 1, games: [], strategies: [], battles: [], researches: [] })

/** 容错：任何历史文件都能读成当前结构。 */
function normalizeLibrary(input) {
  const lib = input && typeof input === 'object' ? input : {}
  return {
    version: 1,
    games: Array.isArray(lib.games) ? lib.games : [],
    strategies: Array.isArray(lib.strategies) ? lib.strategies : [],
    battles: Array.isArray(lib.battles) ? lib.battles : [],
    researches: Array.isArray(lib.researches) ? lib.researches : [],
  }
}

/** 串行化的 JSON 资料库：读走缓存，写走 tmp+rename，避免半截文件。 */
function createStore(file) {
  let cache
  let queue = Promise.resolve()

  const load = async () => {
    if (cache) return cache
    try {
      cache = normalizeLibrary(JSON.parse(await readFile(file, 'utf8')))
    } catch {
      cache = emptyLibrary()
    }
    return cache
  }

  const flush = async () => {
    await mkdir(dirname(file), { recursive: true })
    const tmp = `${file}.tmp`
    await writeFile(tmp, `${JSON.stringify(cache, null, 2)}\n`, 'utf8')
    await rename(tmp, file)
  }

  /** 读事务。 */
  const read = (fn) => {
    const run = queue.then(async () => fn(await load()))
    queue = run.then(
      () => undefined,
      () => undefined,
    )
    return run
  }

  /** 写事务：fn 直接改 cache，返回结果对象。 */
  const mutate = (fn) => {
    const run = queue.then(async () => {
      const lib = await load()
      const result = await fn(lib)
      await flush()
      return result
    })
    queue = run.then(
      () => undefined,
      () => undefined,
    )
    return run
  }

  return { file, load, read, mutate }
}

/* ------------------------------------------------------------------ 领域操作 */

function findGame(lib, key) {
  if (!key) return undefined
  const needle = String(key).trim().toLowerCase()
  return (
    lib.games.find((game) => game.id === key) ??
    lib.games.find((game) => String(game.name).trim().toLowerCase() === needle) ??
    lib.games.find((game) => String(game.name).toLowerCase().includes(needle))
  )
}

function requireGame(lib, key) {
  const game = findGame(lib, key)
  if (!game) {
    const names = lib.games.map((item) => `${item.name}(${item.id})`).join('、') || '（空）'
    throw new Error(`找不到游戏「${key ?? ''}」。现有：${names}。先用 game_coach_game(action:"register") 建档。`)
  }
  return game
}

function gameLine(game) {
  const strategies = game.strategyCount ?? 0
  return `- ${game.name} [${game.id}] 类别=${CATEGORY_LABEL[game.category] ?? game.category} 策略=${strategies}`
}

function gameReport(lib, game) {
  const strategies = lib.strategies.filter((item) => item.gameId === game.id)
  const byTier = TIERS.map((tier) => {
    const rows = strategies.filter((item) => item.tier === tier)
    return `${tier}:${rows.length}`
  }).join(' ')
  const battles = lib.battles.filter((item) => item.gameId === game.id)
  const open = battles.filter((item) => !item.outcome)
  return [
    `游戏 ${game.name} [${game.id}]`,
    `类别: ${CATEGORY_LABEL[game.category] ?? game.category}`,
    `规则: ${clip(game.rules, 600) || '（未记录）'}`,
    game.notes ? `备注: ${clip(game.notes, 400)}` : undefined,
    `策略: 共${strategies.length}条 (${byTier})`,
    `实战: 共${battles.length}回合记录，未结算${open.length}条`,
    strategies.length
      ? ['策略清单:', ...strategies.map((item) => `- [${item.id}] ${item.tier} ${item.title}${item.wins || item.losses ? ` (${item.wins ?? 0}胜/${item.losses ?? 0}负)` : ''}`)].join('\n')
      : '策略清单: （空，建议先研究再入库）',
  ]
    .filter(Boolean)
    .join('\n')
}

function strategyLine(item) {
  const record = item.wins || item.losses ? ` ${item.wins ?? 0}胜/${item.losses ?? 0}负` : ''
  return `- [${item.id}] ${item.tier}${item.phase ? ` ${item.phase}` : ''} ${item.title}${record}`
}

/* ------------------------------------------------------------------ 工具渲染 */

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: false,
    required: ['text'],
    properties: { text: { type: 'string', description: '给模型看的文本结果。' } },
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

const textResult = (text) => ({ text })

/* ------------------------------------------------------------------ 插件本体 */

/**
 * 注册游戏教练的工具、提示段落与面板数据接口。
 * @param ctx - Cordis 上下文。
 * @param config - 可选配置，`dataFile` 覆盖资料库路径。
 */
export function apply(ctx, config) {
  const store = createStore(
    config?.dataFile && typeof config.dataFile === 'string'
      ? config.dataFile
      : join(homedir(), '.dsh', 'game-coach', 'library.json'),
  )

  /* ---------------------------------------------------------------- 游戏档案 */
  ctx.inject(['tools'], (ctx) => {
    ctx.tools.register({
      name: 'game_coach_game',
      description: [
        '游戏教练的第一入口：按游戏建档与查询。类别固定四选一：turn(回合制) / paused(可暂停策略) / rt2d(2D实时) / rt3d(3D实时)。',
        'action=register 新建或覆盖（需 name；建议同时写清 rules：回合结构、胜负条件、资源/行动经济学、当前版本或模组）。',
        'action=list 列出全部游戏（含策略条数）；action=get 看某个游戏的档案与策略清单（id 或名称都可）。',
        'action=update 改档案字段；action=delete 删除游戏及其策略、实战记录。',
        '开始教用户打某个游戏之前先 list；没有档案就先 register，否则后面策略无法归档。',
      ].join('\n'),
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['action'],
        properties: {
          action: { type: 'string', enum: ['register', 'list', 'get', 'update', 'delete'], description: '要执行的操作。' },
          id: { type: 'string', description: 'action=get/update/delete 时的游戏 id（也可填名称）。' },
          name: { type: 'string', description: '游戏名称。' },
          category: { type: 'string', enum: CATEGORIES, description: '游戏类别，默认 turn。' },
          rules: { type: 'string', description: '规则摘要：回合结构、胜负条件、关键资源与限制。写得越具体，后面的策略越准。' },
          notes: { type: 'string', description: '其它备注（平台、版本、难度、模组）。' },
          tags: { type: 'array', items: { type: 'string' }, description: '标签。' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const action = String(args.action ?? '')
        if (action === 'list') {
          return store.read((lib) => {
            if (!lib.games.length) return textResult('还没有任何游戏档案。用 game_coach_game(action:"register") 建档。')
            const lines = lib.games.map((game) => gameLine({ ...game, strategyCount: lib.strategies.filter((item) => item.gameId === game.id).length }))
            return textResult(`共${lib.games.length}个游戏档案：\n${lines.join('\n')}`)
          })
        }
        if (action === 'get') {
          return store.read((lib) => textResult(gameReport(lib, requireGame(lib, args.id ?? args.name))))
        }
        if (action === 'delete') {
          return store.mutate((lib) => {
            const game = requireGame(lib, args.id ?? args.name)
            lib.games = lib.games.filter((item) => item.id !== game.id)
            lib.strategies = lib.strategies.filter((item) => item.gameId !== game.id)
            lib.battles = lib.battles.filter((item) => item.gameId !== game.id)
            lib.researches = lib.researches.filter((item) => item.gameId !== game.id)
            return textResult(`已删除游戏「${game.name}」及其策略、实战记录。`)
          })
        }
        const patch = {}
        if (typeof args.name === 'string' && args.name.trim()) patch.name = args.name.trim()
        if (typeof args.category === 'string' && args.category.trim()) {
          if (!CATEGORIES.includes(args.category)) throw new Error(`category 必须是 ${CATEGORIES.join('/')} 之一。`)
          patch.category = args.category
        }
        if (typeof args.rules === 'string') patch.rules = args.rules
        if (typeof args.notes === 'string') patch.notes = args.notes
        if (Array.isArray(args.tags)) patch.tags = asArray(args.tags)

        return store.mutate((lib) => {
          if (action === 'update') {
            const game = requireGame(lib, args.id ?? args.name)
            Object.assign(game, patch, { updatedAt: now() })
            return textResult(`已更新游戏档案：\n${gameReport(lib, game)}`)
          }
          const existing = args.name ? findGame(lib, args.name) : undefined
          if (existing) {
            Object.assign(existing, patch, { updatedAt: now() })
            return textResult(`已更新既有游戏档案（同名复用）：\n${gameReport(lib, existing)}`)
          }
          if (!patch.name) throw new Error('register 需要 name。')
          const game = {
            id: uid('g'),
            category: 'turn',
            rules: '',
            notes: '',
            tags: [],
            createdAt: now(),
            updatedAt: now(),
            ...patch,
          }
          lib.games.push(game)
          return textResult(`已建档：\n${gameReport(lib, game)}`)
        })
      },
    })

    /* -------------------------------------------------------------- 策略库 */
    ctx.tools.register({
      name: 'game_coach_strategy',
      description: [
        '策略库：研究结论与实战经验的唯一沉淀处，按游戏归档，分 L0/L1/L2 三层。',
        'action=save 保存一条（需 gameId、title、body；tier 默认 L1，phase 可写开局/中期/残局等；source 记来源：research/实战/攻略）。',
        'action=search 检索（gameId 必填，query 匹配标题/正文/标签；tier 可过滤；limit 默认 8）。教用户某个局面之前先 search，命中就复用，别重复造。',
        'action=get 读全文；action=rate 记一次胜负（delta=win/loss），用来淘汰过期条目；action=delete 删除；action=export 导出该游戏的策略包全文。',
        '实战中发现的更优解法要 save 回去，并写清触发条件，避免下次照抄失效。',
      ].join('\n'),
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['action'],
        properties: {
          action: { type: 'string', enum: ['save', 'search', 'get', 'rate', 'delete', 'export'], description: '要执行的操作。' },
          gameId: { type: 'string', description: '所属游戏 id 或名称。' },
          id: { type: 'string', description: '策略 id（get/rate/delete 用）。' },
          title: { type: 'string', description: '一句话策略名，写触发场景。' },
          tier: { type: 'string', enum: TIERS, description: 'L0 稳定套路 / L1 条件化 / L2 实验性。' },
          phase: { type: 'string', description: '适用阶段，如 开局/中期/残局/对线期。' },
          body: { type: 'string', description: '策略正文：触发条件 → 动作序列 → 预期收益 → 失败模式。' },
          source: { type: 'string', description: '来源：research / 实战 / 攻略 / 数据。' },
          tags: { type: 'array', items: { type: 'string' }, description: '标签。' },
          query: { type: 'string', description: '检索关键词。' },
          limit: { type: 'number', description: '返回条数上限，默认 8。' },
          delta: { type: 'string', enum: ['win', 'loss'], description: 'rate 的增减方向。' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const action = String(args.action ?? '')
        if (action === 'search') {
          return store.read((lib) => {
            const game = requireGame(lib, args.gameId)
            const query = String(args.query ?? '').trim().toLowerCase()
            const limit = Number.isFinite(args.limit) ? Math.max(1, Math.min(50, Number(args.limit))) : 8
            let rows = lib.strategies.filter((item) => item.gameId === game.id)
            if (args.tier) rows = rows.filter((item) => item.tier === args.tier)
            if (query) {
              rows = rows.filter((item) =>
                [item.title, item.body, item.phase, ...(item.tags ?? [])]
                  .filter(Boolean)
                  .join(' ')
                  .toLowerCase()
                  .includes(query),
              )
            }
            if (!rows.length) {
              const total = lib.strategies.filter((item) => item.gameId === game.id).length
              return textResult(
                `《${game.name}》没有匹配「${args.query ?? ''}」的策略（该游戏共${total}条）。可以先用 game_coach_research 研究，再 game_coach_strategy(action:"save") 入库。`,
              )
            }
            const picked = rows.slice(0, limit)
            const body = picked.map(
              (item) => `### [${item.id}] ${item.tier} ${item.title}\n阶段: ${item.phase || '-'} 来源: ${item.source || '-'} 战绩: ${item.wins ?? 0}胜/${item.losses ?? 0}负\n${clip(item.body, 700)}`,
            )
            return textResult(`《${game.name}》命中${rows.length}条，显示${picked.length}条：\n\n${body.join('\n\n')}`)
          })
        }
        if (action === 'export') {
          return store.read((lib) => {
            const game = requireGame(lib, args.gameId)
            const rows = lib.strategies.filter((item) => item.gameId === game.id)
            if (!rows.length) return textResult(`《${game.name}》策略库为空。`)
            const blocks = TIERS.map((tier) => {
              const list = rows.filter((item) => item.tier === tier)
              if (!list.length) return ''
              return [`## ${TIER_LABEL[tier]}`, ...list.map((item) => `### ${item.title}\n阶段: ${item.phase || '-'}\n${item.body}`)].join('\n\n')
            }).filter(Boolean)
            return textResult(`# 《${game.name}》策略包\n\n${blocks.join('\n\n')}`)
          })
        }
        if (action === 'get') {
          return store.read((lib) => {
            const item = lib.strategies.find((row) => row.id === args.id)
            if (!item) throw new Error(`找不到策略 ${args.id ?? ''}。`)
            return textResult(
              `[${item.id}] 《${findGame(lib, item.gameId)?.name ?? item.gameId}》 ${item.tier} ${item.title}\n阶段: ${item.phase || '-'} 来源: ${item.source || '-'} 战绩: ${item.wins ?? 0}胜/${item.losses ?? 0}负\n标签: ${(item.tags ?? []).join('/') || '-'}\n\n${item.body}`,
            )
          })
        }
        if (action === 'delete') {
          return store.mutate((lib) => {
            const item = lib.strategies.find((row) => row.id === args.id)
            if (!item) throw new Error(`找不到策略 ${args.id ?? ''}。`)
            lib.strategies = lib.strategies.filter((row) => row.id !== item.id)
            return textResult(`已删除策略「${item.title}」。`)
          })
        }
        if (action === 'rate') {
          return store.mutate((lib) => {
            const item = lib.strategies.find((row) => row.id === args.id)
            if (!item) throw new Error(`找不到策略 ${args.id ?? ''}。`)
            if (args.delta === 'win') item.wins = (item.wins ?? 0) + 1
            else item.losses = (item.losses ?? 0) + 1
            item.updatedAt = now()
            return textResult(`${strategyLine(item)} → 已记为${args.delta === 'win' ? '胜' : '负'}`)
          })
        }
        // save
        const title = String(args.title ?? '').trim()
        const body = String(args.body ?? '').trim()
        if (!title) throw new Error('save 需要 title。')
        if (!body) throw new Error('save 需要 body（写清触发条件→动作→预期收益→失败模式）。')
        return store.mutate((lib) => {
          const game = requireGame(lib, args.gameId)
          const tier = TIERS.includes(args.tier) ? args.tier : 'L1'
          const duplicate = lib.strategies.find(
            (item) => item.gameId === game.id && String(item.title).trim() === title,
          )
          const payload = {
            tier,
            phase: typeof args.phase === 'string' ? args.phase : '',
            body,
            source: typeof args.source === 'string' ? args.source : '',
            tags: Array.isArray(args.tags) ? asArray(args.tags) : [],
            updatedAt: now(),
          }
          if (duplicate) {
            Object.assign(duplicate, payload)
            return textResult(`已覆盖同名策略：\n${strategyLine(duplicate)}`)
          }
          const item = { id: uid('s'), gameId: game.id, title, wins: 0, losses: 0, createdAt: now(), ...payload }
          lib.strategies.push(item)
          return textResult(`已入库《${game.name}》：\n${strategyLine(item)}`)
        })
      },
    })

    /* -------------------------------------------------------------- 实战记录 */
    ctx.tools.register({
      name: 'game_coach_turn',
      description: [
        '实战记录：把每个回合的「局面 → 合法动作 → 决策 → 结果 → 教训」落账，这是实战获取策略的地方。',
        'action=log 记录一个决策回合（需 gameId、state；legalActions 传合法动作列表，decision 必须是其中之一；think 写一句理由，alternatives 写备选与风险）。返回 battleId。',
        'action=finish 结算该回合（battleId 或省略取最近一条未结算），写 outcome(win/loss/neutral) 与 lesson；若教训值得复用，再用 game_coach_strategy(save) 入库。',
        'action=list 查看该游戏最近的实战记录；action=export 导出完整对局复盘。',
        'legalActions 必须来自游戏真实给出的选项；若游戏没有显式动作列表，就由你列出可执行操作并编号，决策仍只能选其中之一。',
      ].join('\n'),
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['action'],
        properties: {
          action: { type: 'string', enum: ['log', 'finish', 'list', 'export'], description: '要执行的操作。' },
          gameId: { type: 'string', description: '游戏 id 或名称。' },
          battleId: { type: 'string', description: '回合记录 id（finish 时用，省略取最近未结算）。' },
          turn: { type: 'number', description: '回合序号，省略则自动＋1。' },
          state: { type: 'string', description: '当前局面摘要（可见信息、资源、对手状态）。' },
          legalActions: { type: 'array', items: { type: 'string' }, description: '合法动作列表，按游戏给出的顺序。' },
          decision: { type: 'string', description: '最终动作（必须来自 legalActions）。' },
          think: { type: 'string', description: '一句决策理由。' },
          alternatives: { type: 'string', description: '备选方案与风险。' },
          outcome: { type: 'string', enum: ['win', 'loss', 'neutral'], description: '该回合结果。' },
          lesson: { type: 'string', description: '教训/可复用结论。' },
          limit: { type: 'number', description: 'list 返回条数，默认 10。' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const action = String(args.action ?? '')
        if (action === 'list') {
          return store.read((lib) => {
            const game = requireGame(lib, args.gameId)
            const limit = Number.isFinite(args.limit) ? Math.max(1, Math.min(100, Number(args.limit))) : 10
            const rows = lib.battles.filter((item) => item.gameId === game.id).slice(-limit)
            if (!rows.length) return textResult(`《${game.name}》还没有实战记录。`)
            return textResult(
              `《${game.name}》最近${rows.length}回合：\n${rows
                .map(
                  (item) =>
                    `- [${item.id}] T${item.turn} 决策: ${clip(item.decision, 80) || '-'} 结果: ${item.outcome || '未结算'}${item.lesson ? ` 教训: ${clip(item.lesson, 100)}` : ''}`,
                )
                .join('\n')}`,
            )
          })
        }
        if (action === 'export') {
          return store.read((lib) => {
            const game = requireGame(lib, args.gameId)
            const rows = lib.battles.filter((item) => item.gameId === game.id)
            if (!rows.length) return textResult(`《${game.name}》还没有实战记录。`)
            return textResult(
              `# 《${game.name}》对局复盘（${rows.length}回合）\n\n${rows
                .map(
                  (item) =>
                    `## T${item.turn} [${item.id}]\n局面: ${item.state || '-'}\n合法动作: ${(item.legalActions ?? []).join(' | ') || '-'}\n决策: ${item.decision || '-'}\n理由: ${item.think || '-'}\n结果: ${item.outcome || '未结算'}\n教训: ${item.lesson || '-'}`,
                )
                .join('\n\n')}`,
            )
          })
        }
        if (action === 'finish') {
          return store.mutate((lib) => {
            const game = requireGame(lib, args.gameId)
            const rows = lib.battles.filter((item) => item.gameId === game.id)
            const target = args.battleId
              ? rows.find((item) => item.id === args.battleId)
              : [...rows].reverse().find((item) => !item.outcome)
            if (!target) throw new Error(`《${game.name}》没有可结算的回合记录。`)
            target.outcome = args.outcome ?? 'neutral'
            if (typeof args.lesson === 'string') target.lesson = args.lesson
            target.closedAt = now()
            return textResult(
              `已结算 T${target.turn} [${target.id}]：${target.outcome}${target.lesson ? `｜教训：${clip(target.lesson, 200)}` : ''}\n若这条经验值得复用，用 game_coach_strategy(action:"save") 入库。`,
            )
          })
        }
        // log
        return store.mutate((lib) => {
          const game = requireGame(lib, args.gameId)
          const legalActions = asArray(args.legalActions)
          const decision = String(args.decision ?? '').trim()
          if (!decision) throw new Error('log 需要 decision（最终动作）。')
          if (legalActions.length && !legalActions.includes(decision)) {
            throw new Error(`decision「${decision}」不在 legalActions 里。合法动作：${legalActions.join(' | ')}`)
          }
          const rows = lib.battles.filter((item) => item.gameId === game.id)
          const turn = Number.isFinite(args.turn) ? Number(args.turn) : rows.length + 1
          const item = {
            id: uid('b'),
            gameId: game.id,
            turn,
            state: String(args.state ?? ''),
            legalActions,
            decision,
            think: String(args.think ?? ''),
            alternatives: String(args.alternatives ?? ''),
            outcome: '',
            lesson: '',
            createdAt: now(),
          }
          lib.battles.push(item)
          return textResult(`已记录 T${turn} [${item.id}]：${decision}${legalActions.length ? `（合法动作${legalActions.length}项）` : ''}`)
        })
      },
    })

    /* -------------------------------------------------------------- 研究任务 */
    ctx.tools.register({
      name: 'game_coach_research',
      description: [
        '研究任务：教用户之前把「要回答的问题、研究计划、结论、来源」写下来，避免随口给建议。',
        'action=save 保存一次研究（需 gameId、question；plan 写研究步骤，findings 写结论，sources 写依据）。结论可复用时拆成策略用 game_coach_strategy(save) 入库。',
        'action=list 看该游戏已经研究过什么，避免重复研究。',
      ].join('\n'),
      parameters: {
        type: 'object',
        additionalProperties: false,
        required: ['action'],
        properties: {
          action: { type: 'string', enum: ['save', 'list'], description: '要执行的操作。' },
          gameId: { type: 'string', description: '游戏 id 或名称。' },
          question: { type: 'string', description: '要回答的问题，例如「第3波之前怎么分配采集」' },
          plan: { type: 'string', description: '研究计划/推理步骤。' },
          findings: { type: 'string', description: '研究结论。' },
          sources: { type: 'string', description: '依据来源。' },
          limit: { type: 'number', description: 'list 返回条数，默认 10。' },
        },
      },
      output: TEXT_OUTPUT,
      async execute(args) {
        const action = String(args.action ?? '')
        if (action === 'list') {
          return store.read((lib) => {
            const game = requireGame(lib, args.gameId)
            const limit = Number.isFinite(args.limit) ? Math.max(1, Math.min(100, Number(args.limit))) : 10
            const rows = lib.researches.filter((item) => item.gameId === game.id).slice(-limit)
            if (!rows.length) return textResult(`《${game.name}》还没有研究记录。`)
            return textResult(
              `《${game.name}》研究记录：\n${rows
                .map((item) => `- [${item.id}] ${clip(item.question, 100)}${item.findings ? `\n  结论: ${clip(item.findings, 200)}` : ''}`)
                .join('\n')}`,
            )
          })
        }
        const question = String(args.question ?? '').trim()
        if (!question) throw new Error('save 需要 question。')
        return store.mutate((lib) => {
          const game = requireGame(lib, args.gameId)
          const item = {
            id: uid('r'),
            gameId: game.id,
            question,
            plan: String(args.plan ?? ''),
            findings: String(args.findings ?? ''),
            sources: String(args.sources ?? ''),
            createdAt: now(),
          }
          lib.researches.push(item)
          return textResult(`已记录研究 [${item.id}]《${game.name}》：${clip(question, 120)}`)
        })
      },
    })
  })

  /* ---------------------------------------------------------------- 提示段落 */
  ctx.inject(['systemPrompt'], (ctx) => {
    ctx.systemPrompt.section({ name: 'game-coach', order: 900, text: PROTOCOL })
  })

  /* ---------------------------------------------------------------- 面板数据 */
  ctx.inject(['webServer'], (ctx) => {
    const sendJson = (res, status, payload) => {
      const body = JSON.stringify(payload)
      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
      res.end(body)
    }

    const readBody = (req) =>
      new Promise((resolve, reject) => {
        const chunks = []
        let size = 0
        req.on('data', (chunk) => {
          size += chunk.length
          if (size > 1_000_000) {
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

    const libraryPayload = async () => {
      const lib = await store.load()
      return {
        file: store.file,
        games: lib.games.map((game) => ({
          ...game,
          strategyCount: lib.strategies.filter((item) => item.gameId === game.id).length,
          battleCount: lib.battles.filter((item) => item.gameId === game.id).length,
        })),
        strategies: lib.strategies,
        battles: lib.battles,
        researches: lib.researches,
      }
    }

    const handle = async (req, res) => {
      const url = new URL(req.url ?? '/', 'http://127.0.0.1')
      const path = url.pathname.replace(/^\/gc6/, '').replace(/\/+$/, '') || '/'
      try {
        if (req.method === 'POST' && path === '/api/diag') {
          const body = await readBody(req)
          try {
            await appendFile('D:\\插件\\game-stack\\diag-theme.json', `${JSON.stringify({ at: now(), body })}\n`, 'utf8')
          } catch {}
          return sendJson(res, 200, { ok: true })
        }
        if (req.method === 'GET' && (path === '/api/library' || path === '/api/library/')) {
          return sendJson(res, 200, await libraryPayload())
        }
        if (req.method === 'POST' && path === '/api/op') {
          const body = await readBody(req)
          const op = String(body?.op ?? '')
          if (op === 'create_game') {
            const name = String(body?.name ?? '').trim()
            if (!name) return sendJson(res, 400, { ok: false, error: '需要游戏名称' })
            const category = CATEGORIES.includes(body?.category) ? body.category : 'turn'
            await store.mutate((lib) => {
              if (findGame(lib, name)) return
              lib.games.push({
                id: uid('g'),
                name,
                category,
                rules: String(body?.rules ?? ''),
                notes: '',
                tags: [],
                createdAt: now(),
                updatedAt: now(),
              })
            })
            return sendJson(res, 200, { ok: true, library: await libraryPayload() })
          }
          if (op === 'delete_game') {
            await store.mutate((lib) => {
              const game = findGame(lib, body?.id)
              if (!game) return
              lib.games = lib.games.filter((item) => item.id !== game.id)
              lib.strategies = lib.strategies.filter((item) => item.gameId !== game.id)
              lib.battles = lib.battles.filter((item) => item.gameId !== game.id)
              lib.researches = lib.researches.filter((item) => item.gameId !== game.id)
            })
            return sendJson(res, 200, { ok: true, library: await libraryPayload() })
          }
          if (op === 'delete_strategy') {
            await store.mutate((lib) => {
              lib.strategies = lib.strategies.filter((item) => item.id !== body?.id)
            })
            return sendJson(res, 200, { ok: true, library: await libraryPayload() })
          }
          if (op === 'rate_strategy') {
            await store.mutate((lib) => {
              const item = lib.strategies.find((row) => row.id === body?.id)
              if (!item) return
              if (body?.delta === 'win') item.wins = (item.wins ?? 0) + 1
              else item.losses = (item.losses ?? 0) + 1
              item.updatedAt = now()
            })
            return sendJson(res, 200, { ok: true, library: await libraryPayload() })
          }
          return sendJson(res, 400, { ok: false, error: `未知操作 ${op}` })
        }
        if (req.method === 'GET' && path === '/api/health') {
          return sendJson(res, 200, { ok: true, file: store.file })
        }
        return sendJson(res, 404, { ok: false, error: 'not found' })
      } catch (error) {
        return sendJson(res, 500, { ok: false, error: String(error?.message ?? error) })
      }
    }

    ctx.webServer.register({ kind: 'prefix', path: '/gc6', handler: handle })
  })
}

