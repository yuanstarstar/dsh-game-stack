/**
 * 游戏学习（合并面板）· 浏览器半边
 *
 * 一个侧边栏入口 + 四个页签：
 *   概览     —— 三个子系统的汇总数字（游戏/策略/规则/闸门）
 *   游戏与策略 —— 知识库（/gc6/api）：建档、策略、胜率、研究
 *   规则与流程 —— 决策引擎（/rule-workbench/api）：规则、测试、流程学习
 *   闸门     —— 防幻觉（/game-guard/api）：契约、派生量、拒止与对账
 *
 * 只读 + 少量写操作；一切判断仍在 host 侧完成。
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-game-learn-v2',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const GC = '/gc6/api'
    const RW = '/rule-workbench/api'
    const GD = '/game-guard/api'

    /* ------------------------------------------------------------ 语言包 */
    const ZH = {
      title: '游戏学习',
      subtitle: '知识库 + 决策引擎 + 防幻觉闸门',
      refresh: '刷新',
      loading: '加载中…',
      failed: '加载失败',
      tabOverview: '概览',
      tabGames: '游戏与策略',
      tabRules: '规则与流程',
      tabGuard: '闸门',
      overviewGames: '游戏数量',
      overviewStrategies: '策略条数',
      overviewRules: '规则条数',
      overviewRefused: '闸门拒止',
      overviewHint: '先建档 → 打一局 → 学流程 → 写规则 → 开闸门。数字都来自本机文件，不是猜的。',
      newGame: '新建游戏',
      gameName: '游戏名字',
      category: '类别',
      rulesField: '规则摘要（回合结构、胜负条件、资源）',
      create: '建档',
      emptyGames: '还没有游戏，先建一个。',
      strategies: '策略',
      researches: '研究',
      battles: '实战',
      winShort: '胜',
      lossShort: '负',
      rateWin: '+胜',
      rateLoss: '+负',
      deleteIt: '删除',
      noStrategy: '这个游戏还没有策略。',
      scope: '域',
      rulesTitle: '规则',
      engineBadge: '引擎算',
      flowLine: '流程',
      flowStages: '阶段',
      learnFlow: '学习流程',
      learned: '已学习',
      draft: '草稿',
      runTitle: '测试台',
      factsLabel: '在这一帧的事实（JSON）',
      run: '运行',
      guardTitle: '读数契约',
      required: '必填字段',
      derived: '派生量',
      checks: '检查',
      refused: '拒止',
      reconciled: '对账',
      mismatched: '不一致',
      none: '无',
      on: '启用',
      off: '停用',
      hits: '命中',
      priority: '优先级',
    }
    const EN = {
      title: 'Game Learning',
      subtitle: 'Knowledge · Decision engine · Guard',
      refresh: 'Refresh',
      loading: 'Loading…',
      failed: 'Failed',
      tabOverview: 'Overview',
      tabGames: 'Games',
      tabRules: 'Rules',
      tabGuard: 'Guard',
      overviewGames: 'Games',
      overviewStrategies: 'Strategies',
      overviewRules: 'Rules',
      overviewRefused: 'Refusals',
      overviewHint: 'Create a game → play one session → learn the flow → write rules → arm the guard.',
      newGame: 'New game',
      gameName: 'Name',
      category: 'Category',
      rulesField: 'Rules summary',
      create: 'Create',
      emptyGames: 'No games yet.',
      strategies: 'Strategies',
      researches: 'Research',
      battles: 'Battles',
      winShort: 'W',
      lossShort: 'L',
      rateWin: '+W',
      rateLoss: '+L',
      deleteIt: 'Delete',
      noStrategy: 'No strategies yet.',
      scope: 'Scope',
      rulesTitle: 'Rules',
      engineBadge: 'engine',
      flowLine: 'Flow',
      flowStages: 'stages',
      learnFlow: 'Learn flow',
      learned: 'learned',
      draft: 'draft',
      runTitle: 'Test bench',
      factsLabel: 'Facts JSON',
      run: 'Run',
      guardTitle: 'Reading contract',
      required: 'Required',
      derived: 'Derived',
      checks: 'Checks',
      refused: 'Refusals',
      reconciled: 'Reconciled',
      mismatched: 'Mismatches',
      none: 'none',
      on: 'on',
      off: 'off',
      hits: 'hits',
      priority: 'priority',
    }

    let dict = ZH
    const translate = (key) => dict[key] ?? key

    /* ------------------------------------------------------------ 样式 */
    const CSS = `
.gl { display: flex; flex-direction: column; gap: 10px; padding: 12px; height: 100%; box-sizing: border-box; font-size: 12px; color: var(--dsw-alias-label-primary); overflow: auto; }
.gl-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.gl-title { font-size: 15px; font-weight: 600; }
.gl-sub { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.gl-spacer { flex: 1; }
.gl-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.gl-tab { padding: 4px 12px; border-radius: 999px; cursor: pointer; font-size: 12px; border: 1px solid var(--dsw-alias-border-l2); background: transparent; color: var(--dsw-alias-label-secondary); }
.gl-tab.on { background: var(--dsw-alias-bg-overlay); color: var(--dsw-alias-label-primary); border-color: var(--dsw-alias-label-secondary); font-weight: 600; }
.gl-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
.gl-card { border: 1px solid var(--dsw-alias-border-l2); border-radius: 10px; padding: 10px; background: var(--dsw-alias-bg-layer-1); }
.gl-num { font-size: 20px; font-weight: 600; }
.gl-lab { font-size: 11px; color: var(--dsw-alias-label-secondary); margin-top: 2px; }
.gl-panel { border: 1px solid var(--dsw-alias-border-l2); border-radius: 10px; padding: 10px; background: var(--dsw-alias-bg-layer-1); }
.gl-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--dsw-alias-border-l2); }
.gl-row:last-child { border-bottom: none; }
.gl-col { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.gl-name { font-weight: 600; }
.gl-meta { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.gl-chip { font-size: 10px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--dsw-alias-border-l2); color: var(--dsw-alias-label-secondary); white-space: nowrap; }
.gl-btn { padding: 3px 10px; border-radius: 8px; cursor: pointer; font-size: 11px; border: 1px solid var(--dsw-alias-border-l2); background: var(--dsw-alias-bg-overlay); color: var(--dsw-alias-label-primary); }
.gl-btn:hover { border-color: var(--dsw-alias-label-secondary); }
.gl-input, .gl-area { font: inherit; color: inherit; background: transparent; border: 1px solid var(--dsw-alias-border-l2); border-radius: 8px; padding: 5px 8px; min-width: 0; }
.gl-area { width: 100%; box-sizing: border-box; resize: vertical; min-height: 90px; font-family: ui-monospace, Consolas, monospace; font-size: 11px; }
.gl-err { color: var(--dsw-alias-state-error-primary); }
.gl-ok { color: var(--dsw-alias-state-success-primary); }
.gl-muted { color: var(--dsw-alias-label-secondary); }
.gl-list { display: flex; flex-direction: column; }
.gl-body { white-space: pre-wrap; line-height: 1.55; max-height: 140px; overflow: auto; border-top: 1px dashed var(--dsw-alias-border-l2); margin-top: 4px; padding-top: 4px; font-size: 11px; }
`

    /* ------------------------------------------------------------ 小工具 */
    const useAsync = (loader, deps) => {
      const [state, setState] = React.useState({ loading: true, data: null, error: '' })
      const [tick, setTick] = React.useState(0)
      const run = React.useCallback(() => setTick((n) => n + 1), [])
      React.useEffect(() => {
        let alive = true
        setState((prev) => ({ ...prev, loading: true, error: '' }))
        loader()
          .then((data) => alive && setState({ loading: false, data, error: '' }))
          .catch((error) => alive && setState({ loading: false, data: null, error: String(error?.message ?? error) }))
        return () => {
          alive = false
        }
      }, [...deps, tick])
      return [state, run]
    }

    const getJson = async (base, path) => {
      const response = await fetch(`${base}${path}`, { credentials: 'same-origin' })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`)
      return payload
    }

    const postJson = async (url, body) => {
      const response = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body ?? {}),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`)
      return payload
    }

    const Button = (label, onClick, extra) =>
      h(
        'button',
        { className: `gl-btn${extra?.danger ? ' gl-err' : ''}`, onClick, title: extra?.title ?? '', type: 'button' },
        label,
      )

    const Cards = (items) =>
      h(
        'div',
        { className: 'gl-cards' },
        items.map((item, index) =>
          h('div', { className: 'gl-card', key: `card-${index}` }, [
            h('div', { className: 'gl-num', key: 'n' }, String(item.value)),
            h('div', { className: 'gl-lab', key: 'l' }, item.label),
          ]),
        ),
      )

    /* ------------------------------------------------------------ 概览 */
    function OverviewTab({ library, rules, guard, reloadAll }) {
      const games = library.data?.games ?? []
      const strategies = games.reduce((sum, game) => sum + (game.strategies?.length ?? 0), 0)
      const ruleCount = rules.data?.rules?.length ?? 0
      const refused = guard.data?.stats?.refused ?? 0
      return h('div', { className: 'gl' }, [
        Cards([
          { label: translate('overviewGames'), value: games.length },
          { label: translate('overviewStrategies'), value: strategies },
          { label: translate('overviewRules'), value: ruleCount },
          { label: translate('overviewRefused'), value: refused },
        ]),
        h('div', { className: 'gl-panel' }, [
          h('div', { className: 'gl-name', key: 't' }, translate('overviewHint')),
          h(
            'div',
            { className: 'gl-meta', key: 'm' },
            games.map((game) => `${game.name}（${game.category}，策略 ${game.strategies?.length ?? 0}）`).join('，') || translate('emptyGames'),
          ),
        ]),
        h('div', { className: 'gl-tabs' }, [Button(translate('refresh'), reloadAll)]),
      ])
    }

    /* ------------------------------------------------------------ 游戏与策略 */
    function GamesTab({ library, reload }) {
      const games = library.data?.games ?? []
      const [name, setName] = React.useState('')
      const [category, setCategory] = React.useState('turn')
      const [rules, setRules] = React.useState('')
      const [open, setOpen] = React.useState('')
      const [busy, setBusy] = React.useState(false)
      const [notice, setNotice] = React.useState('')

      const op = async (payload) => {
        setBusy(true)
        try {
          await postJson(`${GC}/op`, payload)
          setNotice('已保存')
          reload()
        } catch (error) {
          setNotice(String(error?.message ?? error))
        } finally {
          setBusy(false)
        }
      }

      const create = async () => {
        if (!name.trim()) return
        await op({ op: 'create_game', name: name.trim(), category, rules: rules.trim() })
        setName('')
        setRules('')
      }

      const rows = games.map((game) => {
        const isOpen = open === game.id
        const strategies = game.strategies ?? []
        return h('div', { className: 'gl-panel', key: game.id, style: { marginBottom: '8px' } }, [
          h('div', { className: 'gl-row', key: 'head' }, [
            h('div', { className: 'gl-col' }, [
              h('div', { className: 'gl-name' }, game.name),
              h(
                'div',
                { className: 'gl-meta' },
                `${game.category} ｜ ${translate('strategies')} ${strategies.length} ｜ ${translate('researches')} ${game.researches?.length ?? 0} ｜ ${translate('battles')} ${game.battles?.length ?? 0}`,
              ),
            ]),
            Button(isOpen ? '收起' : '展开', () => setOpen(isOpen ? '' : game.id)),
          ]),
          isOpen
            ? h('div', { className: 'gl-list', key: 'list' }, [
                game.rules ? h('div', { className: 'gl-body', key: 'rules' }, game.rules) : null,
                strategies.length === 0
                  ? h('div', { className: 'gl-meta', key: 'empty' }, translate('noStrategy'))
                  : strategies.map((item) =>
                      h('div', { className: 'gl-row', key: item.id }, [
                        h('div', { className: 'gl-col' }, [
                          h('div', { className: 'gl-name' }, `${item.tier ?? ''} ${item.title}`.trim()),
                          h(
                            'div',
                            { className: 'gl-meta' },
                            `${item.phase ?? ''} ｜ ${translate('winShort')}${item.outcomes?.win ?? 0} ${translate('lossShort')}${item.outcomes?.loss ?? 0}`,
                          ),
                          item.body ? h('div', { className: 'gl-body' }, item.body) : null,
                        ]),
                        Button(translate('rateWin'), () => op({ op: 'rate_strategy', id: item.id, delta: 'win' })),
                        Button(translate('rateLoss'), () => op({ op: 'rate_strategy', id: item.id, delta: 'loss' })),
                        Button(translate('deleteIt'), () => op({ op: 'delete_strategy', id: item.id }), { danger: true }),
                      ]),
                    ),
              ])
            : null,
        ])
      })

      return h('div', { className: 'gl' }, [
        h('div', { className: 'gl-panel', key: 'form' }, [
          h('div', { className: 'gl-name' }, translate('newGame')),
          h('div', { className: 'gl-row' }, [
            h('input', {
              className: 'gl-input',
              value: name,
              placeholder: translate('gameName'),
              onChange: (event) => setName(event.target.value),
            }),
            h(
              'select',
              { className: 'gl-input', value: category, onChange: (event) => setCategory(event.target.value) },
              [
                h('option', { value: 'turn', key: 't' }, 'turn 回合制'),
                h('option', { value: 'paused', key: 'p' }, 'paused 可暂停策略'),
                h('option', { value: 'rt2d', key: 'a' }, 'rt2d 2D 实时'),
                h('option', { value: 'rt3d', key: 'b' }, 'rt3d 3D 实时'),
              ],
            ),
            Button(translate('create'), create),
          ]),
          h('textarea', {
            className: 'gl-area',
            value: rules,
            placeholder: translate('rulesField'),
            onChange: (event) => setRules(event.target.value),
          }),
        ]),
        notice ? h('div', { className: 'gl-meta', key: 'notice' }, notice) : null,
        rows.length ? h('div', { key: 'rows' }, rows) : h('div', { className: 'gl-meta', key: 'empty' }, translate('emptyGames')),
        busy ? h('div', { className: 'gl-meta' }, translate('loading')) : null,
      ])
    }

    /* ------------------------------------------------------------ 规则与流程 */
    const SAMPLE_RULE = {
      id: 'demo.kill-low',
      priority: 100,
      when: { all: [{ scene: 'battle' }, { path: 'enemy.hp', op: '<=', value: 20 }] },
      do: [{ type: 'click_entity', target: 'enemy' }],
      expect: { summary: '打掉残血', changes: { 'enemy.hp': -12 }, risk: '低' },
      why: '敌方残血时先补刀，减少回合消耗',
      on: 'win',
    }

    function RulesTab({ scope, payload, reload }) {
      const rules = payload.data?.rules ?? []
      const stats = payload.data?.stats ?? { runs: 0, matched: 0, unmatched: 0 }
      const flow = payload.data?.flow ?? null
      const scopes = payload.data?.scopes ?? []
      const [editor, setEditor] = React.useState(JSON.stringify(SAMPLE_RULE, null, 2))
      const [facts, setFacts] = React.useState('{\n  "scene": "battle",\n  "enemy": { "hp": 12 }\n}')
      const [result, setResult] = React.useState(null)
      const [notice, setNotice] = React.useState('')
      const [busy, setBusy] = React.useState(false)

      const post = async (path, body) => {
        setBusy(true)
        try {
          const data = await postJson(`${RW}${path}`, body)
          setNotice('已提交')
          reload()
          return data
        } catch (error) {
          setNotice(String(error?.message ?? error))
          return null
        } finally {
          setBusy(false)
        }
      }

      const save = async () => {
        try {
          const parsed = JSON.parse(editor)
          await post('/save', { scope, rules: [parsed] })
        } catch (error) {
          setNotice(`保存失败：${error?.message ?? error}`)
        }
      }

      const runRule = async () => {
        try {
          const parsed = JSON.parse(facts)
          const data = await post('/run', { scope, facts: parsed })
          setResult(data)
        } catch (error) {
          setNotice(`运行失败：${error?.message ?? error}`)
        }
      }

      return h('div', { className: 'gl' }, [
        h('div', { className: 'gl-row', key: 'meta' }, [
          h('span', { className: 'gl-chip' }, `${translate('engineBadge')} ｜ ${scope}`),
          h('span', { className: 'gl-meta' }, `跑 ${stats.runs}，命中 ${stats.matched}，未覆盖 ${stats.unmatched}`),
          h('span', { className: 'gl-spacer' }),
          h('span', { className: 'gl-meta' }, `${translate('flowLine')} ${flow?.phases ?? 0} ${translate('flowStages')}${flow?.draft ? `（${translate('draft')}）` : flow ? `（${translate('learned')}）` : ''}`),
          Button(translate('learnFlow'), () => post('/flow/learn', { scope })),
        ]),
        scopes.length ? h('div', { className: 'gl-meta', key: 'scopes' }, `${translate('scope')}：${scopes.join('、')}`) : null,
        h('div', { className: 'gl-list', key: 'rules' }, [
          h('div', { className: 'gl-name', key: 'h' }, translate('rulesTitle')),
          rules.length === 0
            ? h('div', { className: 'gl-meta', key: 'empty' }, '还没有规则。')
            : rules.map((rule) =>
                h('div', { className: 'gl-row', key: rule.id }, [
                  h('div', { className: 'gl-col' }, [
                    h('div', { className: 'gl-name' }, rule.id),
                    h(
                      'div',
                      { className: 'gl-meta' },
                      `${translate('priority')} ${rule.priority} ｜ ${translate('hits')} ${rule.hits ?? 0} ｜ 胜${rule.outcomes?.win ?? 0}负${rule.outcomes?.loss ?? 0} ｜ ${rule.enabled === false ? translate('off') : translate('on')}`,
                    ),
                    rule.why ? h('div', { className: 'gl-meta' }, rule.why) : null,
                  ]),
                  Button(translate('off'), () => post('/toggle', { scope, id: rule.id, enabled: rule.enabled === false })),
                  Button(translate('deleteIt'), () => post('/delete', { scope, id: rule.id }), { danger: true }),
                ]),
              ),
        ]),
        h('div', { className: 'gl-panel', key: 'editor' }, [
          h('div', { className: 'gl-name' }, `${translate('rulesTitle')} JSON`),
          h('textarea', { className: 'gl-area', value: editor, onChange: (event) => setEditor(event.target.value) }),
          h('div', { className: 'gl-tabs' }, [Button('保存', save)]),
        ]),
        h('div', { className: 'gl-panel', key: 'bench' }, [
          h('div', { className: 'gl-name' }, translate('runTitle')),
          h('div', { className: 'gl-meta' }, translate('factsLabel')),
          h('textarea', { className: 'gl-area', value: facts, onChange: (event) => setFacts(event.target.value) }),
          h('div', { className: 'gl-tabs' }, [Button(translate('run'), runRule)]),
          result
            ? h('div', { className: 'gl-body', key: 'out' }, JSON.stringify(result.decision ?? result, null, 2))
            : null,
        ]),
        notice ? h('div', { className: 'gl-meta', key: 'notice' }, notice) : null,
        busy ? h('div', { className: 'gl-meta' }, translate('loading')) : null,
      ])
    }

    /* ------------------------------------------------------------ 闸门 */
    function GuardTab({ scope, payload }) {
      const contract = payload.data?.contract ?? {}
      const stats = payload.data?.stats ?? {}
      const required = contract.required ?? []
      const derived = contract.derived ?? []
      const [editor, setEditor] = React.useState(JSON.stringify({ required, derived }, null, 2))
      const [notice, setNotice] = React.useState('')
      const [busy, setBusy] = React.useState(false)

      const save = async () => {
        setBusy(true)
        try {
          const schema = JSON.parse(editor)
          await postJson(`${GD}/schema`, { scope, schema })
          setNotice('契约已保存')
        } catch (error) {
          setNotice(`保存失败：${error?.message ?? error}`)
        } finally {
          setBusy(false)
        }
      }

      return h('div', { className: 'gl' }, [
        Cards([
          { label: translate('checks'), value: stats.checks ?? 0 },
          { label: translate('refused'), value: stats.refused ?? 0 },
          { label: translate('reconciled'), value: stats.reconciled ?? 0 },
          { label: translate('mismatched'), value: stats.mismatched ?? 0 },
        ]),
        h('div', { className: 'gl-panel', key: 'contract' }, [
          h('div', { className: 'gl-name' }, translate('guardTitle')),
          h('div', { className: 'gl-meta' }, `${translate('required')}：${required.length ? required.join('、') : translate('none')}`),
          h(
            'div',
            { className: 'gl-meta' },
            `${translate('derived')}：${derived.length ? derived.map((item) => `${item.path} = ${item.expr}`).join('；') : translate('none')}`,
          ),
          contract.maxAgeMs ? h('div', { className: 'gl-meta' }, `帧龄上限 ${contract.maxAgeMs} ms`) : null,
        ]),
        h('div', { className: 'gl-panel', key: 'editor' }, [
          h('div', { className: 'gl-name' }, '契约 JSON'),
          h('textarea', { className: 'gl-area', value: editor, onChange: (event) => setEditor(event.target.value) }),
          h('div', { className: 'gl-tabs' }, [Button('保存', save)]),
        ]),
        notice ? h('div', { className: 'gl-meta', key: 'notice' }, notice) : null,
        busy ? h('div', { className: 'gl-meta' }, translate('loading')) : null,
      ])
    }

    /* ------------------------------------------------------------ 面板 */
    function Page() {
      const [tab, setTab] = React.useState('overview')
      const [scope, setScope] = React.useState('default')
      const [draftScope, setDraftScope] = React.useState('default')

      const library = useAsync(() => getJson(GC, '/library'), [])
      const rules = useAsync(() => getJson(RW, `/list?scope=${encodeURIComponent(scope)}`), [scope])
      const guard = useAsync(() => getJson(GD, `/guard?scope=${encodeURIComponent(scope)}`), [scope])

      const reloadAll = () => {
        library[1]()
        rules[1]()
        guard[1]()
      }

      const error = library[0].error || rules[0].error || guard[0].error

      const tabs = [
        ['overview', translate('tabOverview')],
        ['games', translate('tabGames')],
        ['rules', translate('tabRules')],
        ['guard', translate('tabGuard')],
      ]

      let body = null
      if (tab === 'overview') body = h(OverviewTab, { library: library[0], rules: rules[0], guard: guard[0], reloadAll })
      if (tab === 'games') body = h(GamesTab, { library: library[0], reload: library[1] })
      if (tab === 'rules') body = h(RulesTab, { scope, payload: rules[0], reload: rules[1] })
      if (tab === 'guard') body = h(GuardTab, { scope, payload: guard[0] })

      return h('div', { className: 'gl' }, [
        h('style', { key: 'css' }, CSS),
        h('div', { className: 'gl-head', key: 'head' }, [
          h('div', { className: 'gl-col' }, [
            h('div', { className: 'gl-title', key: 't' }, translate('title')),
            h('div', { className: 'gl-sub', key: 's' }, translate('subtitle')),
          ]),
          h('span', { className: 'gl-spacer' }),
          h('input', {
            className: 'gl-input',
            style: { width: '110px' },
            value: draftScope,
            title: translate('scope'),
            onChange: (event) => setDraftScope(event.target.value),
            onKeyDown: (event) => {
              if (event.key === 'Enter') setScope(draftScope.trim() || 'default')
            },
          }),
          Button(translate('refresh'), reloadAll),
        ]),
        h(
          'div',
          { className: 'gl-tabs', key: 'tabs' },
          tabs.map(([id, label]) =>
            h(
              'button',
              {
                key: id,
                type: 'button',
                className: `gl-tab${tab === id ? ' on' : ''}`,
                onClick: () => setTab(id),
              },
              label,
            ),
          ),
        ),
        error ? h('div', { className: 'gl-err', key: 'err' }, `${translate('failed')}: ${error}`) : null,
        body,
      ])
    }

    /* ------------------------------------------------------------ 注册 */
    function SidebarIcon() {
      return h(
        'svg',
        { width: 18, height: 18, viewBox: '0 0 18 18', fill: 'none', stroke: 'currentColor', strokeWidth: 1.5 },
        h('path', { d: 'M9 2.5l6 3.2v6.6L9 15.5 3 12.3V5.7L9 2.5z' }),
        h('path', { d: 'M6.2 8.6l1.8 1.8 3.8-3.6' }),
      )
    }

    return {
      name: 'game-learn',
      inject: ['slots', 'locale'],
      apply(ctx) {
        try {
          ctx.locale.register('game-learn', 'zh', ZH)
          ctx.locale.register('game-learn', 'en', EN)
        } catch (error) {
          console.warn('[game-learn] locale register failed', error)
        }

        try {
          const bound = ctx.locale.bind('game-learn')
          dict = new Proxy(ZH, {
            get(target, key) {
              if (typeof key !== 'string') return target[key]
              try {
                const value = bound(key)
                if (value !== undefined && value !== null && value !== key) return value
              } catch (error) {}
              return target[key]
            },
          })
        } catch (error) {
          console.warn('[game-learn] locale bind failed, keeping inline labels', error)
        }

        ctx.slots.inject('sidebar.panellist', () =>
          ctx.slots.register({ name: 'sidebar.panellist', id: 'game-learn', order: 24, label: () => translate('title') }, SidebarIcon),
        )
        ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'game-learn' }, Page))
      },
    }
  },
})
