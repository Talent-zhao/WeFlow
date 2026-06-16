import { spawn, type ChildProcess } from 'child_process'
import path from 'path'
import fs from 'fs'
import net from 'net'
import { app, clipboard, type BrowserWindow } from 'electron'
import type { BotStatus, BotEvent, BotConfig, BotStats } from '../../src/types/electron'
import { chatService } from './chatService'
import { ConfigService } from './config'

const DEFAULT_WS_PORT = 9877
const MAX_LOG_BUFFER = 1000
const RECONNECT_DELAY_MS = 2000
const MAX_RECONNECT_ATTEMPTS = 5
const WCDB_POLL_INTERVAL_MS = 2000
const MESSAGES_PER_SESSION = 3

export function resolveBotDir(): string {
  const candidates = [
    // Production: bundled in resources/bot/
    path.join(process.resourcesPath || '', 'resources', 'bot'),
    // Development: running from project directory
    path.join(__dirname, '..', '..', 'resources', 'bot'),
    // Development: electron/services -> electron -> project root -> resources/bot
    path.join(__dirname, '..', 'resources', 'bot'),
  ]
  for (const p of candidates) {
    try { if (fs.existsSync(p)) return p } catch {}
  }
  // Fallback — show error when bot tries to start
  return candidates[0]
}

function detectPython(): string {
  const { execSync } = require('child_process')

  // On Windows, try known Python commands
  if (process.platform === 'win32') {
    const commands = ['python', 'python3', 'py -3', 'py']
    for (const cmd of commands) {
      try {
        const output = execSync(`${cmd} --version 2>&1`, { encoding: 'utf8', timeout: 5000 })
        if (output.toLowerCase().includes('python')) {
          console.log(`[bot] Found Python: ${cmd} → ${output.trim()}`)
          return cmd
        }
      } catch {}
    }

    // Try common install paths
    const localAppData = process.env.LOCALAPPDATA || ''
    const searchPaths = [
      path.join(localAppData, 'Programs', 'Python'),
      'C:\\Python312', 'C:\\Python311', 'C:\\Python310', 'C:\\Python313',
    ]
    for (const base of searchPaths) {
      const exe = path.join(base, 'python.exe')
      try { if (fs.existsSync(exe)) { console.log(`[bot] Found Python at: ${exe}`); return exe } } catch {}
    }

    // Scan %LOCALAPPDATA%\Programs\Python for versioned dirs
    try {
      const pythonBase = path.join(localAppData, 'Programs', 'Python')
      if (fs.existsSync(pythonBase)) {
        for (const entry of fs.readdirSync(pythonBase)) {
          const exe = path.join(pythonBase, entry, 'python.exe')
          if (fs.existsSync(exe)) { console.log(`[bot] Found Python at: ${exe}`); return exe }
        }
      }
    } catch {}

    // Try PATH-based search
    try {
      const where = execSync('where python 2>&1', { encoding: 'utf8' })
      const lines = where.trim().split('\n').filter((l: string) => l)
      if (lines.length > 0) { console.log(`[bot] Found Python via where: ${lines[0]}`); return lines[0] }
    } catch {}
  }

  // macOS / Linux
  for (const cmd of ['python3', 'python']) {
    try {
      execSync(`which ${cmd}`, { encoding: 'utf8' })
      console.log(`[bot] Found Python: ${cmd}`)
      return cmd
    } catch {}
  }

  return process.platform === 'win32' ? 'python' : 'python3'
}

interface BotBridgeOptions {
  pythonPath?: string
  botDir?: string
  wsPort?: number
}

export class BotBridgeService {
  private ws: any = null  // globalThis.WebSocket in Node.js 22+
  private pythonProcess: ChildProcess | null = null
  private mainWindow: BrowserWindow | null = null
  private options: Required<BotBridgeOptions>
  private logBuffer: BotEvent[] = []
  private status: BotStatus = {
    state: 'stopped',
    uptime: 0,
    scanCount: 0,
    messagesReceived: 0,
    repliesSent: 0,
    errors: 0,
    apiCalls: 0,
    wsConnected: false,
    pythonRunning: false,
  }
  private stats: BotStats = {
    scanCount: 0,
    messagesReceived: 0,
    repliesSent: 0,
    errors: 0,
    apiCalls: 0,
    uptime: 0,
  }
  private config: BotConfig = {
    baseUrl: '',
    model: '',
    persona: 'zhaoyoucai',
    replyDelayMin: 1.0,
    replyDelayMax: 4.0,
    replyCooldown: 12.0,
    pollInterval: 2.0,
    unreadScanInterval: 3.0,
    pythonPath: 'python',
    wsPort: DEFAULT_WS_PORT,
    listenMode: 'all',
    targetContacts: [],
    blacklist: [],
  }
  private reconnectAttempts = 0
  private disposed = false
  private wcdbPollTimer: NodeJS.Timeout | null = null
  private uptimeTimer: NodeJS.Timeout | null = null
  private startedAt = 0
  private lastCheckedPerSession = new Map<string, number>()
  private chatServiceReady = false
  private processedMessages = new Set<string>()  // "sessionId:localId"

  constructor(options: BotBridgeOptions = {}) {
    const resolvedBotDir = options.botDir || resolveBotDir()
    const detectedPython = options.pythonPath || detectPython()

    this.options = {
      pythonPath: detectedPython,
      botDir: resolvedBotDir,
      wsPort: options.wsPort || DEFAULT_WS_PORT,
    }

    this.config.pythonPath = detectedPython
  }

  setMainWindow(win: BrowserWindow) {
    this.mainWindow = win
  }

  // ── Public API ──

  async start(): Promise<{ success: boolean; error?: string }> {
    if (this.pythonProcess) {
      return { success: false, error: 'Bot is already running' }
    }

    try {
      // Read shared AI config from the unified settings
      const configService = ConfigService.getInstance()
      const aiApiKey = configService.get('aiModelApiKey')
      const aiModel = configService.get('aiModelApiModel')
      const aiBaseUrl = configService.get('aiModelApiBaseUrl')

      if (!aiApiKey) {
        return { success: false, error: '请先在设置 → AI → 基础配置中配置 API 密钥' }
      }

      // Sync shared config into internal config for UI display
      this.config.baseUrl = aiBaseUrl
      this.config.model = aiModel || 'gpt-4o-mini'
      this.config.persona = configService.get('botPersona') || 'zhaoyoucai'
      this.config.listenMode = (configService.get('botListenMode') as any) || 'all'
      this.config.targetContacts = configService.get('botTargetContacts') || []
      this.config.blacklist = configService.get('botBlacklist') || []

      // Ensure chatService is connected
      const dbResult = await chatService.ensureConnected()
      if (!dbResult.success) {
        return { success: false, error: '数据库未连接，请先在首页连接微信数据库' }
      }
      this.chatServiceReady = true

      // Verify bot scripts exist
      const botScript = path.join(this.options.botDir, 'bot_reply_server.py')
      if (!fs.existsSync(botScript)) {
        return {
          success: false,
          error: `智能回复脚本未找到: ${botScript}\n请确保程序安装完整`,
        }
      }

      // Quick check that Python is functional
      try {
        require('child_process').execSync(`"${this.options.pythonPath}" --version 2>&1`, { encoding: 'utf8' })
      } catch {
        return {
          success: false,
          error: `Python 未找到或无法运行: ${this.options.pythonPath}\n请安装 Python 3.10+ 并将其添加到系统 PATH，\n然后安装依赖: pip install anthropic openai pyautogui pyperclip pywin32`,
        }
      }

      const port = await this.findAvailablePort(this.options.wsPort)
      this.config.wsPort = port

      // Spawn the lightweight reply engine with shared AI config as env vars
      this.pythonProcess = spawn(this.options.pythonPath, [
        botScript,
        '--ws-port', String(port),
      ], {
        cwd: this.options.botDir,
        stdio: ['pipe', 'pipe', 'pipe'],
        env: {
          ...process.env,
          ANTHROPIC_API_KEY: aiApiKey,
          ANTHROPIC_MODEL: aiModel || 'gpt-4o-mini',
          ANTHROPIC_BASE_URL: aiBaseUrl || '',
          PERSONA: this.config.persona || 'zhaoyoucai',
          PERSONAS_DIR: path.join(app.getPath('userData'), 'personas'),
          CONTACTS_PATH: path.join(app.getPath('userData'), 'bot', 'contacts.json'),
        },
      })

      this.pythonProcess.stdout?.on('data', (data: Buffer) => {
        const text = data.toString().trim()
        if (!text) return
        console.log('[bot stdout]', text)
      })

      this.pythonProcess.stderr?.on('data', (data: Buffer) => {
        const text = data.toString().trim()
        if (!text) return
        console.warn('[bot stderr]', text)
        // Forward Python errors to the renderer so users can see API failures
        for (const line of text.split('\n')) {
          const trimmed = line.trim()
          if (!trimmed) continue
          const isError = /\[ERROR\]|Traceback|Error:/i.test(trimmed)
          this.pushEvent({
            type: isError ? 'error' : 'info',
            subtype: 'python_stderr',
            message: trimmed.slice(0, 300),
          })
          if (this.mainWindow && !this.mainWindow.isDestroyed()) {
            this.mainWindow.webContents.send('bot:event', {
              type: isError ? 'error' : 'info',
              subtype: 'python_stderr',
              message: trimmed.slice(0, 300),
              ts: Date.now() / 1000,
            } as BotEvent)
          }
        }
      })

      this.pythonProcess.on('exit', (code) => {
        console.log(`[bot] Python process exited with code ${code}`)
        this.pythonProcess = null
        this.stopWcdbPolling()
        this.stopUptimeTimer()
        this.updateStatus({ pythonRunning: false, state: 'stopped' })
        this.sendToRenderer('bot:statusChange', this.status)
      })

      this.pythonProcess.on('error', (err) => {
        console.error('[bot] Python process error:', err)
        this.pythonProcess = null
        this.stopWcdbPolling()
        this.stopUptimeTimer()
        this.updateStatus({ pythonRunning: false, state: 'error' })
        this.pushEvent({ type: 'error', message: `Python 进程错误: ${err.message}` })
        this.sendToRenderer('bot:statusChange', this.status)
      })

      this.updateStatus({ pythonRunning: true, state: 'running' })
      this.startUptimeTimer()
      this.sendToRenderer('bot:statusChange', this.status)

      // Connect WebSocket after a short delay to let Python server start
      await new Promise(resolve => setTimeout(resolve, 1500))
      await this.connectWebSocket(port)

      // Start WCDB polling for new messages
      this.startWcdbPolling()

      return { success: true }
    } catch (err: any) {
      return { success: false, error: err.message }
    }
  }

  async stop(): Promise<{ success: boolean; error?: string }> {
    try {
      this.stopWcdbPolling()
      this.stopUptimeTimer()
      this.sendCommand({ command: 'stop' })

      await new Promise(resolve => setTimeout(resolve, 800))

      if (this.pythonProcess) {
        this.pythonProcess.kill('SIGTERM')
        setTimeout(() => {
          if (this.pythonProcess) {
            this.pythonProcess.kill('SIGKILL')
            this.pythonProcess = null
          }
        }, 3000)
      }

      this.disconnectWebSocket()
      this.updateStatus({ pythonRunning: false, wsConnected: false, state: 'stopped' })
      this.sendToRenderer('bot:statusChange', this.status)
      return { success: true }
    } catch (err: any) {
      return { success: false, error: err.message }
    }
  }

  async pause(): Promise<{ success: boolean; error?: string }> {
    this.stopWcdbPolling()
    this.pauseUptimeTimer()
    this.updateStatus({ state: 'paused' })
    this.sendToRenderer('bot:statusChange', this.status)
    if (this.ws && this.ws.readyState === 1) {
      this.sendCommand({ command: 'pause' })
    }
    return { success: true }
  }

  async resume(): Promise<{ success: boolean; error?: string }> {
    if (this.chatServiceReady) {
      this.startWcdbPolling()
    }
    this.startUptimeTimer(this.status.uptime)
    this.updateStatus({ state: 'running' })
    this.sendToRenderer('bot:statusChange', this.status)
    if (this.ws && this.ws.readyState === 1) {
      this.sendCommand({ command: 'resume' })
    }
    return { success: true }
  }

  getStatus(): BotStatus {
    if (this.startedAt > 0 && this.status.state === 'running') {
      const elapsed = Math.floor((Date.now() - this.startedAt) / 1000)
      return { ...this.status, uptime: elapsed }
    }
    return { ...this.status }
  }
  getConfig(): BotConfig {
    const configService = ConfigService.getInstance()
    return {
      ...this.config,
      baseUrl: String(configService.get('aiModelApiBaseUrl') || this.config.baseUrl || '').trim(),
      model: String(configService.get('aiModelApiModel') || this.config.model || '').trim(),
      persona: String(configService.get('botPersona') || this.config.persona || 'zhaoyoucai').trim(),
      listenMode: (configService.get('botListenMode') as any) || this.config.listenMode || 'all',
      targetContacts: configService.get('botTargetContacts') || this.config.targetContacts || [],
      blacklist: configService.get('botBlacklist') || this.config.blacklist || [],
    }
  }

  async setConfig(key: string, value: any): Promise<{ success: boolean; error?: string }> {
    (this.config as any)[key] = value
    if (key === 'pythonPath') {
      this.options.pythonPath = value
    }
    // Persist contact filter settings
    if (key === 'listenMode') {
      ConfigService.getInstance().set('botListenMode', value as any)
    } else if (key === 'targetContacts') {
      ConfigService.getInstance().set('botTargetContacts', value as any)
    } else if (key === 'blacklist') {
      ConfigService.getInstance().set('botBlacklist', value as any)
    }
    if (this.ws && this.ws.readyState === 1) {
      this.sendCommand({ command: 'config', key, value })
    }
    return { success: true }
  }

  getStats(): BotStats { return { ...this.stats } }

  getLogs(limit?: number): BotEvent[] {
    if (limit && limit > 0) return this.logBuffer.slice(-limit)
    return [...this.logBuffer]
  }

  clearLogs(): { success: boolean } {
    this.logBuffer = []
    return { success: true }
  }

  async testConnection(): Promise<{ success: boolean; error?: string }> {
    if (!this.ws || this.ws.readyState !== 1) {
      return { success: false, error: 'WebSocket not connected' }
    }
    return { success: true }
  }

  async sendMessage(contact: string, message: string): Promise<{ success: boolean; error?: string }> {
    if (!message || !message.trim()) {
      return { success: false, error: '消息内容不能为空' }
    }

    // Try WebSocket first if bot is connected
    if (this.ws && this.ws.readyState === 1) {
      try {
        this.sendCommand({
          command: 'send_message',
          contact: contact || '',
          message: message.trim(),
        })
        return { success: true }
      } catch (err: any) {
        // Fall through to direct send
        console.warn('[bot] WS send failed, falling back to direct:', err.message)
      }
    }

    // Direct clipboard send without bot
    return this.sendViaClipboard(contact, message.trim())
  }

  private sendViaClipboard(contact: string, message: string): { success: boolean; error?: string } {
    try {
      const koffi = require('koffi')
      const user32 = koffi.load('user32.dll')

      const SW_RESTORE = 9
      const VK_CONTROL = 0x11
      const VK_V = 0x56
      const VK_RETURN = 0x0D
      const VK_ESCAPE = 0x1B
      const VK_F = 0x46
      const VK_A = 0x41
      const VK_BACK = 0x08
      const VK_MENU = 0x12
      const KEYEVENTF_KEYUP = 0x0002

      const FindWindowW = user32.func('FindWindowW', 'void*', ['uint16*', 'uint16*'])
      const IsIconic = user32.func('IsIconic', 'bool', ['void*'])
      const ShowWindow = user32.func('ShowWindow', 'bool', ['void*', 'int'])
      const SetForegroundWindow = user32.func('SetForegroundWindow', 'bool', ['void*'])
      const keybd_event = user32.func('keybd_event', 'void', ['uint8', 'uint8', 'uint32', 'uintptr_t'])

      const wait = (ms: number) => {
        const start = Date.now()
        while (Date.now() - start < ms) { /* busy-wait */ }
      }

      // Find WeChat window
      const hwnd = FindWindowW(null, Buffer.from('微信\0', 'ucs2'))
      if (!hwnd) {
        return { success: false, error: '未找到微信窗口，请先打开微信' }
      }

      // Restore if minimized
      if (IsIconic(hwnd)) {
        ShowWindow(hwnd, SW_RESTORE)
      }

      // Release stuck modifier keys (Ctrl/Alt/Shift)
      for (const vk of [VK_CONTROL, VK_MENU, 0x10]) {
        keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
      }
      wait(50)

      // Alt-key trick: Windows grants foreground rights after a modifier keypress
      keybd_event(VK_MENU, 0, 0, 0)
      keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
      wait(30)

      // Bring WeChat to foreground
      SetForegroundWindow(hwnd)
      wait(250)

      // Save original clipboard
      const oldClipboard = clipboard.readText()

      // ── Navigate to contact's chat ──
      if (contact) {
        // Ctrl+F to open WeChat search
        keybd_event(VK_CONTROL, 0, 0, 0)
        keybd_event(VK_F, 0, 0, 0)
        keybd_event(VK_F, 0, KEYEVENTF_KEYUP, 0)
        keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        wait(350)

        // Select any existing search text and replace with contact name
        clipboard.writeText(contact)
        wait(50)

        keybd_event(VK_CONTROL, 0, 0, 0)
        keybd_event(VK_V, 0, 0, 0)
        keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        wait(700)  // Wait for search results to populate

        // Press Enter to open the first search result (the contact's chat)
        keybd_event(VK_RETURN, 0, 0, 0)
        keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
        wait(500)  // Wait for chat to load and input to focus
      }

      // ── Send the message ──
      clipboard.writeText(message)

      // Simulate Ctrl+V
      keybd_event(VK_CONTROL, 0, 0, 0)
      keybd_event(VK_V, 0, 0, 0)
      keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
      keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

      wait(80)

      // Simulate Enter
      keybd_event(VK_RETURN, 0, 0, 0)
      keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)

      // Restore old clipboard after a brief delay
      setTimeout(() => {
        if (oldClipboard) {
          clipboard.writeText(oldClipboard)
        }
      }, 300)

      return { success: true }
    } catch (err: any) {
      return { success: false, error: `直接发送失败: ${err.message || err}` }
    }
  }

  async dispose() {
    this.disposed = true
    await this.stop()
  }

  // ── WCDB message polling ──

  private startWcdbPolling() {
    if (this.wcdbPollTimer || this.disposed) return
    console.log('[bot] Starting WCDB message polling')

    // Seed: mark all existing messages as seen
    this.seedExistingMessages().then(() => {
      this.wcdbPollTimer = setInterval(() => {
        this.pollWcdbForNewMessages()
      }, WCDB_POLL_INTERVAL_MS)
    }).catch((err) => {
      console.error('[bot] Failed to seed existing messages:', err)
      this.wcdbPollTimer = setInterval(() => {
        this.pollWcdbForNewMessages()
      }, WCDB_POLL_INTERVAL_MS)
    })
  }

  private stopWcdbPolling() {
    if (this.wcdbPollTimer) {
      clearInterval(this.wcdbPollTimer)
      this.wcdbPollTimer = null
    }
  }

  private startUptimeTimer(initialUptime = 0) {
    this.stopUptimeTimer()
    this.startedAt = Date.now() - initialUptime * 1000
    this.status.uptime = initialUptime
    this.stats.uptime = initialUptime
    this.uptimeTimer = setInterval(() => {
      if (this.disposed) return
      const elapsed = Math.floor((Date.now() - this.startedAt) / 1000)
      this.status.uptime = elapsed
      this.stats.uptime = elapsed
    }, 1000)
  }

  private stopUptimeTimer() {
    if (this.uptimeTimer) {
      clearInterval(this.uptimeTimer)
      this.uptimeTimer = null
    }
    this.startedAt = 0
    this.status.uptime = 0
    this.stats.uptime = 0
  }

  private pauseUptimeTimer() {
    if (this.uptimeTimer) {
      clearInterval(this.uptimeTimer)
      this.uptimeTimer = null
    }
  }

  private async seedExistingMessages() {
    try {
      const result = await chatService.getSessions()
      if (!result.success || !result.sessions) return

      const now = Date.now()
      for (const session of result.sessions) {
        const sessionId = session.username || session.id
        if (!sessionId) continue
        try {
          const msgResult = await chatService.getLatestMessages(sessionId, 3)
          if (msgResult.success && msgResult.messages) {
            for (const msg of msgResult.messages) {
              this.processedMessages.add(`${sessionId}:${msg.localId}`)
            }
          }
        } catch {} // skip sessions that fail
        this.lastCheckedPerSession.set(sessionId, now)
      }
      console.log(`[bot] Seeded ${this.processedMessages.size} existing messages`)
    } catch (err) {
      console.error('[bot] Error seeding messages:', err)
    }
  }

  private shouldReply(username: string): boolean {
    if (this.config.listenMode === 'all') return true
    if (this.config.listenMode === 'blacklist') {
      return !this.config.blacklist.includes(username)
    }
    if (this.config.listenMode === 'specific') {
      return this.config.targetContacts.includes(username)
    }
    return true
  }

  private async pollWcdbForNewMessages() {
    if (this.disposed || !this.chatServiceReady) return
    if (!this.ws || this.ws.readyState !== 1) return

    try {
      const result = await chatService.getSessions()
      if (!result.success || !result.sessions) return

      let newCount = 0

      for (const session of result.sessions) {
        if (this.disposed) break

        const sessionId = session.username || session.id
        if (!sessionId) continue

        // Filter by listen mode
        if (!this.shouldReply(sessionId)) continue

        try {
          const msgResult = await chatService.getLatestMessages(sessionId, MESSAGES_PER_SESSION)
          if (!msgResult.success || !msgResult.messages) continue

          for (const msg of msgResult.messages) {
            const msgKey = `${sessionId}:${msg.localId}`
            if (this.processedMessages.has(msgKey)) continue

            this.processedMessages.add(msgKey)

            // Only process incoming messages (isSend === 0)
            if (msg.isSend !== 0) continue

            // Skip messages without content
            if (!msg.parsedContent || msg.parsedContent.trim().length === 0) continue

            const contact = session.displayName || session.remark || session.nickname || sessionId
            newCount += 1

            // Clean up processed set periodically
            if (this.processedMessages.size > 10000) {
              const entries = Array.from(this.processedMessages).slice(-5000)
              this.processedMessages = new Set(entries)
            }

            console.log(`[bot] New message from ${contact}: ${msg.parsedContent.slice(0, 50)}`)
            this.pushEvent({
              type: 'scan',
              total: newCount,
              red_dots: 0,
              ts_hits: newCount,
            })

            // Send reply request to Python engine
            this.sendCommand({
              command: 'reply',
              contact: contact,
              displayName: contact,
              message: msg.parsedContent,
              sessionId: sessionId,
              localId: msg.localId,
            })
          }
        } catch { /* skip problematic sessions */ }
      }

      if (newCount > 0) {
        this.sendToRenderer('bot:statsUpdate', this.stats)
      }
    } catch (err) {
      console.error('[bot] WCDB poll error:', err)
    }
  }

  // ── Private helpers (WebSocket, process, IPC) ──

  private async connectWebSocket(port: number): Promise<void> {
    if (this.disposed) return
    this.disconnectWebSocket()

    return new Promise((resolve) => {
      const url = `ws://127.0.0.1:${port}`
      console.log(`[bot] Connecting WebSocket to ${url}`)

      try {
        const ws = new WebSocket(url)
        this.ws = ws

        ws.addEventListener('open', () => {
          console.log('[bot] WebSocket connected')
          this.reconnectAttempts = 0
          this.updateStatus({ wsConnected: true })
          this.sendToRenderer('bot:statusChange', this.status)
          resolve()
        })

        ws.addEventListener('message', (msg: MessageEvent) => {
          try {
            const raw = typeof msg.data === 'string' ? msg.data : Buffer.from(msg.data).toString()
            const botEvent = JSON.parse(raw) as BotEvent
            this.pushEvent(botEvent)

            if (botEvent.type === 'status') {
              const state = (botEvent.state || 'running') as BotStatus['state']
              this.updateStatus({ state, uptime: botEvent.uptime || 0 })
              this.sendToRenderer('bot:statusChange', this.status)
            }

            this.sendToRenderer('bot:event', botEvent)
          } catch { /* ignore */ }
        })

        ws.addEventListener('close', () => {
          console.log('[bot] WebSocket disconnected')
          this.updateStatus({ wsConnected: false })
          this.sendToRenderer('bot:statusChange', this.status)
          this.ws = null

          if (!this.disposed && this.pythonProcess && this.reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            this.reconnectAttempts++
            console.log(`[bot] Reconnecting in ${RECONNECT_DELAY_MS}ms (attempt ${this.reconnectAttempts})`)
            setTimeout(() => this.connectWebSocket(port), RECONNECT_DELAY_MS)
          }
        })

        ws.addEventListener('error', (err: Event) => {
          console.error('[bot] WebSocket error:', (err as any).message || err.type)
          this.ws = null
          resolve()
        })
      } catch (err: any) {
        console.error('[bot] WebSocket connection failed:', err.message)
        resolve()
      }
    })
  }

  private disconnectWebSocket() {
    if (this.ws) {
      try { this.ws.close() } catch {}
      this.ws = null
    }
  }

  private sendCommand(cmd: Record<string, any>) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(cmd))
    }
  }

  private updateStatus(partial: Partial<BotStatus>) {
    this.status = { ...this.status, ...partial }
  }

  private pushEvent(event: BotEvent) {
    this.logBuffer.push(event)
    if (this.logBuffer.length > MAX_LOG_BUFFER) {
      this.logBuffer.splice(0, this.logBuffer.length - MAX_LOG_BUFFER)
    }
    switch (event.type) {
      case 'scan': this.stats.scanCount += 1; break
      case 'msg_received': this.stats.messagesReceived += 1; break
      case 'reply_sent': this.stats.repliesSent += 1; this.stats.apiCalls += 1; break
      case 'error': this.stats.errors += 1; break
    }
  }

  private sendToRenderer(channel: string, data: any) {
    if (this.mainWindow && !this.mainWindow.isDestroyed()) {
      this.mainWindow.webContents.send(channel, data)
    }
  }

  private findAvailablePort(preferred: number): Promise<number> {
    return new Promise((resolve) => {
      const server = net.createServer()
      server.listen(preferred, '127.0.0.1', () => {
        server.close(() => resolve(preferred))
      })
      server.on('error', () => {
        const fallback = net.createServer()
        fallback.listen(0, '127.0.0.1', () => {
          const addr = fallback.address()
          const port = typeof addr === 'object' && addr ? addr.port : preferred + 1
          fallback.close(() => resolve(port))
        })
      })
    })
  }
}
