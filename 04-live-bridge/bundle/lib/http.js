import { clip } from './util.js'

const PREFIX = '/dsh-live-bridge/api'

function json(res, status, value) {
  const body = JSON.stringify(value)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.setHeader('content-length', Buffer.byteLength(body))
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > 512 * 1024) {
        reject(new Error('请求体太大'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim()
      if (!raw) return resolve({})
      try {
        return resolve(JSON.parse(raw))
      } catch {
        return reject(new Error('请求不是合法 JSON'))
      }
    })
    req.on('error', reject)
  })
}

export function createHttpHandler({ store, danmaku, tts, login, logger }) {
  const handler = async (req, res, next) => {
    const url = new URL(req.url, 'http://127.0.0.1')
    const route = url.pathname.slice(PREFIX.length) || '/'
    if (!url.pathname.startsWith(PREFIX)) {
      if (typeof next === 'function') return next()
      return json(res, 404, { ok: false, error: 'not found' })
    }
    if (req.method === 'OPTIONS') {
      res.statusCode = 204
      return res.end()
    }
    try {
      if (req.method === 'GET' && route === '/config') {
        const cfg = store.getConfig()
        return json(res, 200, { ok: true, config: cfg, hasVoiceKey: tts.available(), bili: login.status() })
      }
      if (req.method === 'POST' && route === '/config') {
        const patch = await readBody(req)
        if (patch?.voice?.apiKey !== undefined) {
          store.saveVoiceCredential(String(patch.voice.apiKey || '').trim())
          delete patch.voice.apiKey
        }
        const cfg = store.patchConfig(patch || {})
        if (cfg.autoConnect && cfg.roomId) {
          danmaku.start(cfg.roomId).catch((error) => logger.warn(`重连弹幕失败：${error?.message || error}`))
        }
        return json(res, 200, { ok: true, config: cfg })
      }
      if (req.method === 'GET' && route === '/status') {
        return json(res, 200, {
          ok: true,
          danmaku: { ...danmaku.status, buffered: danmaku.items.length },
          total: danmaku.status.total || 0,
          bili: login.status(),
          hasVoiceKey: tts.available(),
        })
      }
      if (req.method === 'POST' && route === '/danmaku/connect') {
        const body = await readBody(req)
        const roomId = Number(body.roomId || store.getConfig().roomId || 0)
        if (roomId) store.patchConfig({ roomId })
        await danmaku.start(roomId)
        return json(res, 200, { ok: true, status: danmaku.status })
      }
      if (req.method === 'POST' && route === '/danmaku/stop') {
        danmaku.stop()
        return json(res, 200, { ok: true })
      }
      if (req.method === 'GET' && route === '/danmaku/recent') {
        const limit = Number(url.searchParams.get('limit') || 30)
        return json(res, 200, { ok: true, items: danmaku.recent(limit), status: danmaku.status })
      }
      if (req.method === 'POST' && route === '/auth/qr') {
        return json(res, 200, await login.start())
      }
      if (req.method === 'GET' && route === '/auth/poll') {
        return json(res, 200, await login.poll(String(url.searchParams.get('key') || '')))
      }
      if (req.method === 'POST' && route === '/auth/logout') {
        return json(res, 200, login.logout())
      }
      if (req.method === 'POST' && route === '/voice/test') {
        const body = await readBody(req)
        const result = await tts.speak(String(body.text || '能听见我说话吗？'))
        return json(res, 200, result)
      }
      return json(res, 404, { ok: false, error: `未知接口 ${req.method} ${route}` })
    } catch (error) {
      const message = clip(error?.message || String(error), 300)
      logger.warn(`直播桥 HTTP ${req.method} ${route} 失败：${message}`)
      return json(res, 400, { ok: false, error: message })
    }
  }
  return { path: PREFIX, handler }
}
