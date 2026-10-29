// 直播桥 · 设置页：直播间 / B 站登录 / 语音 key，三块。
window.__ModuleLoader__.load({
  id: '@local/dsh-live-bridge',
  factory(require) {
    const React = require('react')
    const { useEffect, useState } = React
    const h = React.createElement

    const API = '/dsh-live-bridge/api'

    const C = {
      text: 'var(--dsw-alias-label-primary, #e8e8ee)',
      sub: 'var(--dsw-alias-label-secondary, #b8b8c4)',
      faint: 'var(--dsw-alias-label-tertiary, #8d8d9c)',
      border: 'var(--dsw-alias-border-primary, rgba(255,255,255,.12))',
      bg: 'var(--dsw-alias-bg-secondary, #191920)',
      input: 'var(--dsw-alias-bg-primary, #101016)',
      accent: 'var(--dsw-alias-brand-primary, #6aa9ff)',
      danger: 'var(--dsw-alias-state-error-primary, #ff6b6b)',
      ok: 'var(--dsw-alias-state-success-primary, #59d18b)',
    }

    const S = {
      card: { border: `1px solid ${C.border}`, borderRadius: '10px', padding: '14px', background: C.bg, marginBottom: '12px' },
      title: { fontSize: '13px', fontWeight: 700, color: C.text, marginBottom: '8px' },
      row: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' },
      input: { background: C.input, color: C.text, border: `1px solid ${C.border}`, borderRadius: '6px', padding: '7px 9px', fontSize: '12px', outline: 'none', width: '100%', boxSizing: 'border-box' },
      small: { color: C.faint, fontSize: '11px', lineHeight: 1.7 },
      btn: { border: `1px solid ${C.border}`, background: 'transparent', color: C.text, borderRadius: '6px', padding: '6px 10px', fontSize: '12px', cursor: 'pointer' },
      primary: { border: `1px solid ${C.accent}`, background: C.accent, color: '#08111f', borderRadius: '6px', padding: '6px 10px', fontSize: '12px', cursor: 'pointer', fontWeight: 600 },
      list: { border: `1px solid ${C.border}`, borderRadius: '8px', padding: '6px 8px', maxHeight: '200px', overflow: 'auto', marginTop: '8px' },
      line: { display: 'flex', gap: '8px', padding: '3px 0', borderBottom: `1px dashed ${C.border}`, fontSize: '12px', color: C.text, wordBreak: 'break-all' },
      qr: { width: '180px', height: '180px', background: '#fff', borderRadius: '8px', padding: '6px' },
    }

    async function api(path, options = {}) {
      const init = { method: options.method || 'GET', headers: { 'content-type': 'application/json' }, cache: 'no-store' }
      if (options.body !== undefined) init.body = JSON.stringify(options.body)
      let response
      try {
        response = await fetch(`${API}${path}`, init)
      } catch {
        throw new Error('连不上直播桥后端：插件可能还没启动，或刚重启没就绪')
      }
      const raw = await response.text().catch(() => '')
      let data = null
      try {
        data = raw ? JSON.parse(raw) : null
      } catch {
        throw new Error(`后端返回的不是 JSON：${raw.slice(0, 120)}`)
      }
      if (!data?.ok) throw new Error(data?.error || `请求失败 ${response.status}`)
      return data
    }

    function LiveBridgePage() {
      const [config, setConfig] = useState(null)
      const [status, setStatus] = useState(null)
      const [roomInput, setRoomInput] = useState('')
      const [voiceKey, setVoiceKey] = useState('')
      const [qr, setQr] = useState(null)
      const [recent, setRecent] = useState([])
      const [notice, setNotice] = useState('')
      const [error, setError] = useState('')

      const reload = async () => {
        const cfg = await api('/config')
        setConfig(cfg.config)
        setRoomInput(String(cfg.config.roomId || ''))
        const st = await api('/status')
        setStatus(st)
        const list = await api('/danmaku/recent?limit=20')
        setRecent(list.items || [])
      }

      useEffect(() => {
        reload().catch((e) => setError(e.message))
        const timer = setInterval(() => {
          api('/status').then(setStatus).catch(() => {})
          api('/danmaku/recent?limit=20').then((d) => setRecent(d.items || [])).catch(() => {})
        }, 4000)
        return () => clearInterval(timer)
      }, [])

      const saveRoom = async () => {
        setError('')
        try {
          const body = { roomId: Number(roomInput) || 0 }
          await api('/config', { method: 'POST', body })
          await api('/danmaku/connect', { method: 'POST', body: { roomId: Number(roomInput) || 0 } })
          setNotice('直播间已保存，正在连弹幕')
          await reload()
        } catch (e) {
          setError(e.message)
        }
      }

      const startQr = async () => {
        setError('')
        try {
          const data = await api('/auth/qr', { method: 'POST' })
          setQr(data)
          setNotice('二维码已生成，用 B 站 App 扫一下')
        } catch (e) {
          setError(e.message)
        }
      }

      useEffect(() => {
        if (!qr?.key) return undefined
        const timer = setInterval(async () => {
          try {
            const data = await api(`/auth/poll?key=${encodeURIComponent(qr.key)}`)
            if (data.state === 'ok') {
              setNotice(data.message)
              setQr(null)
              await reload()
            } else if (data.state === 'expired') {
              setNotice(data.message)
              setQr(null)
            }
          } catch (e) {
            setError(e.message)
          }
        }, 2000)
        return () => clearInterval(timer)
      }, [qr?.key])

      const saveVoice = async () => {
        setError('')
        try {
          await api('/config', { method: 'POST', body: { voice: { apiKey: voiceKey } } })
          setVoiceKey('')
          setNotice('语音 key 已保存（单独存在 data/voice_credentials.json）')
          await reload()
        } catch (e) {
          setError(e.message)
        }
      }

      const testVoice = async () => {
        setError('')
        try {
          setNotice('正在合成并播放…')
          const data = await api('/voice/test', { method: 'POST', body: { text: '我是大肥鱼，能听见我说话吗？' } })
          setNotice(`念完了：${data.voice}，约 ${data.seconds} 秒`)
        } catch (e) {
          setError(e.message)
        }
      }

      const logout = async () => {
        try {
          const data = await api('/auth/logout', { method: 'POST' })
          setNotice(data.message)
          await reload()
        } catch (e) {
          setError(e.message)
        }
      }

      if (!config) return h('div', { style: S.small }, error || '正在加载直播桥…')

      const bili = status?.bili || {}
      const dan = status?.danmaku || {}

      return h('div', { style: { display: 'flex', flexDirection: 'column', width: '100%' } },
        h('div', { style: { ...S.small, marginBottom: '8px' } }, '直播桥只做两件事：读 B 站弹幕、让大肥鱼说话。工具：live_danmaku_read / live_speak。'),
        error ? h('div', { style: { color: C.danger, fontSize: '12px', marginBottom: '8px' } }, error) : null,
        notice ? h('div', { style: { color: C.ok, fontSize: '12px', marginBottom: '8px' } }, notice) : null,

        h('div', { style: S.card },
          h('div', { style: S.title }, '直播间'),
          h('div', { style: S.row },
            h('input', { style: { ...S.input, width: '160px' }, value: roomInput, placeholder: '直播间号', onChange: (e) => setRoomInput(e.target.value) }),
            h('button', { style: S.primary, onClick: saveRoom }, '保存并连接'),
          ),
          h('div', { style: S.small },
            dan.connected ? `已连接房间 ${dan.roomId}｜本地缓冲 ${dan.buffered || 0} 条｜累计 ${status?.total || 0} 条` : `未连接${dan.roomId ? `（房间 ${dan.roomId}）` : ''}${dan.error ? `：${dan.error}` : ''}`,
          ),
          h('div', { style: S.list },
            recent.length
              ? recent.slice(-20).map((item, index) => h('div', { style: S.line, key: `${item.ts}-${index}` },
                h('span', { style: { color: C.faint } }, item.at),
                h('span', { style: { color: C.accent } }, item.uname),
                h('span', null, item.text)))
              : h('div', { style: S.small }, '还没有弹幕。'),
          ),
        ),

        h('div', { style: S.card },
          h('div', { style: S.title }, 'B 站登录（凭证单独存）'),
          h('div', { style: S.small }, bili.loggedIn ? `已登录：${bili.uname || bili.mid}` : '未登录'),
          qr?.svg ? h('div', { style: { marginTop: '8px' }, dangerouslySetInnerHTML: { __html: qr.svg } }) : null,
          h('div', { style: { ...S.row, marginTop: '8px' } },
            h('button', { style: S.primary, onClick: startQr }, qr?.svg ? '重新生成二维码' : '扫码登录'),
            h('button', { style: S.btn, onClick: logout }, '退出登录'),
          ),
          h('div', { style: S.small }, '凭证只存插件自己的 data/bili_credentials.json，不跟直播项目共用。'),
        ),

        h('div', { style: S.card },
          h('div', { style: S.title }, '语音'),
          h('div', { style: S.small }, status?.hasVoiceKey ? '已配置火山引擎 API Key' : '未配置 API Key（不能说话）'),
          h('div', { style: { ...S.row, marginTop: '8px' } },
            h('input', { style: { ...S.input, width: '280px' }, value: voiceKey, placeholder: '粘贴火山引擎 API Key', type: 'password', onChange: (e) => setVoiceKey(e.target.value) }),
            h('button', { style: S.primary, onClick: saveVoice }, '保存 key'),
            h('button', { style: S.btn, onClick: testVoice }, '试听一句'),
          ),
          h('div', { style: S.small }, 'key 单独存 data/voice_credentials.json；音色默认「撒娇学妹」。'),
        ),
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('settings.section', () => ctx.slots.register({
          name: 'settings.section',
          id: 'dsh-live-bridge',
          order: 64,
          label: () => '直播桥',
        }, LiveBridgePage))
      },
    }
  },
})
