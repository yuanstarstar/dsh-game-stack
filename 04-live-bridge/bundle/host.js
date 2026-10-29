import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { Store } from './lib/store.js'
import { DanmakuClient } from './lib/danmaku.js'
import { Tts } from './lib/tts.js'
import { createLogin } from './lib/login.js'
import { createHttpHandler } from './lib/http.js'
import { registerTools } from './lib/tools.js'

export const inject = ['tools', 'webServer']

const ROOT = path.dirname(fileURLToPath(import.meta.url))

export function apply(ctx) {
  const logger = ctx.logger
  const store = new Store(ROOT)
  const danmaku = new DanmakuClient({ store, logger })
  const tts = new Tts({ store, logger })
  const login = createLogin({ store, logger, root: ROOT })

  registerTools(ctx, { store, danmaku, tts, login })

  const http = createHttpHandler({ store, danmaku, tts, login, logger })
  ctx.effect(() => ctx.webServer.register({ kind: 'prefix', path: http.path, handler: http.handler }), 'live-bridge: http')

  const cfg = store.getConfig()
  if (cfg.autoConnect && cfg.roomId) {
    danmaku.start(cfg.roomId).catch((error) => logger.warn(`弹幕自动连接失败：${error?.message || error}`))
  }

  ctx.effect(() => () => danmaku.stop(), 'live-bridge: dispose')
  logger.info('直播桥已加载：工具 live_danmaku_read / live_speak；设置页「直播桥」')
}
