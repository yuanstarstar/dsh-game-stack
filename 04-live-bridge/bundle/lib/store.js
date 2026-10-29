import fs from 'node:fs'
import path from 'node:path'

const DEFAULTS = {
  roomId: 0,
  autoConnect: true,
  bufferSize: 200,
  voice: {
    voice: '撒娇学妹',
    speed: 0,
    pitch: 0,
    resource: 'seed-tts-2.0',
    maxChars: 140,
  },
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return fallback
  }
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8')
  fs.renameSync(tmp, file)
}

function merge(base, patch) {
  const out = { ...base }
  for (const [key, value] of Object.entries(patch || {})) {
    if (value && typeof value === 'object' && !Array.isArray(value) && base[key] && typeof base[key] === 'object') {
      out[key] = merge(base[key], value)
    } else {
      out[key] = value
    }
  }
  return out
}

export class Store {
  constructor(root) {
    this.root = root
    this.dataDir = path.join(root, 'data')
    this.configPath = path.join(this.dataDir, 'config.json')
    this.biliPath = path.join(this.dataDir, 'bili_credentials.json')
    this.voicePath = path.join(this.dataDir, 'voice_credentials.json')
    this.config = merge(DEFAULTS, readJson(this.configPath, {}))
  }

  getConfig() {
    return this.config
  }

  patchConfig(patch) {
    this.config = merge(this.config, patch || {})
    writeJson(this.configPath, this.config)
    return this.config
  }

  /** B 站登录凭证：独立文件，不跟 live 项目共用。 */
  biliCredential() {
    const data = readJson(this.biliPath, {})
    return {
      cookie: String(data.cookie || ''),
      uname: String(data.uname || ''),
      mid: Number(data.mid || 0) || 0,
      updated: Number(data.updated || 0) || 0,
    }
  }

  saveBiliCredential({ cookie, uname, mid }) {
    const value = { cookie: String(cookie || ''), uname: String(uname || ''), mid: Number(mid || 0) || 0, updated: Date.now() }
    writeJson(this.biliPath, value)
    return value
  }

  clearBiliCredential() {
    try {
      fs.rmSync(this.biliPath, { force: true })
    } catch {
      /* ignore */
    }
  }

  /** 语音 key：另一份独立文件。 */
  voiceCredential() {
    const data = readJson(this.voicePath, {})
    return { apiKey: String(data.apiKey || ''), updated: Number(data.updated || 0) || 0 }
  }

  saveVoiceCredential(apiKey) {
    const value = { apiKey: String(apiKey || ''), updated: Date.now() }
    writeJson(this.voicePath, value)
    return value
  }

  cookies() {
    const pairs = {}
    for (const part of this.biliCredential().cookie.split(';')) {
      const index = part.indexOf('=')
      if (index > 0) pairs[part.slice(0, index).trim()] = part.slice(index + 1).trim()
    }
    return pairs
  }
}
