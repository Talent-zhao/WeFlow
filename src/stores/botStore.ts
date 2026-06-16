import { create } from 'zustand'
import type { BotStatus, BotEvent, BotConfig, BotStats } from '../types/electron'

const MAX_LOG_ENTRIES = 500

interface BotState {
  status: BotStatus
  config: BotConfig
  stats: BotStats
  logs: BotEvent[]
  isWsConnected: boolean

  setStatus: (status: Partial<BotStatus>) => void
  addEvent: (event: BotEvent) => void
  updateStats: (stats: Partial<BotStats>) => void
  setConfig: (config: Partial<BotConfig>) => void
  setWsConnected: (connected: boolean) => void
  clearLogs: () => void
}

export const useBotStore = create<BotState>((set) => ({
  status: {
    state: 'stopped',
    uptime: 0,
    scanCount: 0,
    messagesReceived: 0,
    repliesSent: 0,
    errors: 0,
    apiCalls: 0,
    wsConnected: false,
    pythonRunning: false,
  },
  config: {
    baseUrl: '',
    model: '',
    persona: 'zhaoyoucai',
    replyDelayMin: 1.0,
    replyDelayMax: 4.0,
    replyCooldown: 12.0,
    pollInterval: 2.0,
    unreadScanInterval: 3.0,
    pythonPath: 'python',
    wsPort: 9877,
    listenMode: 'all',
    targetContacts: [],
    blacklist: [],
  },
  stats: {
    scanCount: 0,
    messagesReceived: 0,
    repliesSent: 0,
    errors: 0,
    apiCalls: 0,
    uptime: 0,
  },
  logs: [],
  isWsConnected: false,

  setStatus: (partial) =>
    set((state) => ({
      status: { ...state.status, ...partial },
    })),

  addEvent: (event) =>
    set((state) => {
      const newStats = { ...state.stats }
      switch (event.type) {
        case 'scan':
          newStats.scanCount += 1
          break
        case 'msg_received':
          newStats.messagesReceived += 1
          break
        case 'reply_sent':
          newStats.repliesSent += 1
          newStats.apiCalls += 1
          break
        case 'error':
          newStats.errors += 1
          break
      }
      const logs = [...state.logs, event]
      if (logs.length > MAX_LOG_ENTRIES) {
        logs.splice(0, logs.length - MAX_LOG_ENTRIES)
      }
      return { logs, stats: newStats }
    }),

  updateStats: (partial) =>
    set((state) => ({
      stats: { ...state.stats, ...partial },
    })),

  setConfig: (partial) =>
    set((state) => ({
      config: { ...state.config, ...partial },
    })),

  setWsConnected: (connected) =>
    set({ isWsConnected: connected }),

  clearLogs: () => set({ logs: [] }),
}))
