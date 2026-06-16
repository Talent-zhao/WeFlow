import { useEffect, useState, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Play, Square, Pause, RefreshCw, Bot, Send, AlertCircle,
  MessageSquare, Zap, Clock, Settings, Terminal, Trash2,
  Loader2, Search, ChevronDown, Check
} from 'lucide-react'
import { useBotStore } from '../stores/botStore'
import { Avatar } from '../components/Avatar'
import type { BotEvent, BotConfig, BotStatus } from '../types/electron'
import './BotPage.scss'

type Tab = 'log' | 'config' | 'stats'

function formatTime(ts?: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function formatUptime(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = Math.floor(secs % 60)
  if (h > 0) return `${h}时${m}分${s}秒`
  if (m > 0) return `${m}分${s}秒`
  return `${s}秒`
}

const EVENT_ICONS: Record<string, { icon: string; cls: string }> = {
  msg_received: { icon: '←', cls: 'event-msg-received' },
  reply_sent: { icon: '→', cls: 'event-reply-sent' },
  scan: { icon: '🔍', cls: 'event-scan' },
  error: { icon: '✗', cls: 'event-error' },
  status: { icon: '●', cls: 'event-status' },
  info: { icon: 'ℹ', cls: 'event-info' },
  heartbeat: { icon: '♥', cls: 'event-heartbeat' },
  chat_switch: { icon: '📌', cls: 'event-chat-switch' },
  config_updated: { icon: '⚙', cls: 'event-config' },
}

function eventLabel(event: BotEvent): string {
  switch (event.type) {
    case 'msg_received': return `收到 ${event.contact || ''} 的消息`
    case 'reply_sent': return `回复 ${event.contact || ''}`
    case 'scan': return `扫描: ${event.total || 0} 个结果 (🔴${event.red_dots || 0} 🕐${event.ts_hits || 0})`
    case 'error': return event.message || '错误'
    case 'status': return `状态: ${event.state || ''}`
    case 'info': return event.message || event.subtype || '信息'
    case 'chat_switch': return `切换: ${event.contact || ''} [${event.index || 0}/${event.total || 0}]`
    case 'heartbeat': return `心跳 #${event.scan_count || event.scanCount || 0}`
    case 'config_updated': return `配置更新: ${event.key}=${event.value}`
    default: return event.type
  }
}

export default function BotPage() {
  const {
    status, config, stats, logs,
    setStatus, addEvent, updateStats, setConfig, clearLogs,
  } = useBotStore()

  const [activeTab, setActiveTab] = useState<Tab>('log')
  const navigate = useNavigate()
  const [configDirty, setConfigDirty] = useState(false)
  const logEndRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const startedAtRef = useRef<number>(0)
  const uptimeIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const accumulatedUptimeRef = useRef<number>(0)

  // Contact filter state
  const [allContacts, setAllContacts] = useState<{ username: string; displayName: string; avatarUrl?: string; type: string }[]>([])
  const [botFilterSearchKeyword, setBotFilterSearchKeyword] = useState('')
  const [listenModeDropdownOpen, setListenModeDropdownOpen] = useState(false)

  // Subscribe to push events from main process
  useEffect(() => {
    const unsubEvent = window.electronAPI.bot.onEvent((event: BotEvent) => {
      addEvent(event)
      if (event.type === 'status') {
        const newState = (event.state as BotStatus['state']) || 'running'
        if (newState === 'stopped' || newState === 'error') {
          setStatus({ state: newState, uptime: 0 })
        } else {
          // Frontend manages uptime independently during running/paused
          setStatus({ state: newState })
        }
      }
    })
    const unsubStatus = window.electronAPI.bot.onStatusChange((s: any) => {
      if (s.state === 'stopped' || s.state === 'error') {
        setStatus({ ...s, uptime: 0 })
      } else {
        // Frontend manages uptime independently during running/paused
        const { uptime: _, ...rest } = s
        setStatus(rest)
      }
    })

    // Fetch initial state
    Promise.all([
      window.electronAPI.bot.getStatus(),
      window.electronAPI.bot.getConfig(),
      window.electronAPI.bot.getStats(),
      window.electronAPI.bot.getLogs(200),
    ]).then(([s, c, st, l]) => {
      setStatus(s)
      setConfig(c)
      updateStats(st)
      l.forEach((e: BotEvent) => addEvent(e))
    })

    return () => { unsubEvent(); unsubStatus() }
  }, [])

  // Local uptime counter — ticks while running, freezes on pause, resets on stop
  useEffect(() => {
    if (status.state === 'running') {
      const baseUptime = accumulatedUptimeRef.current || status.uptime || 0
      startedAtRef.current = Date.now() - baseUptime * 1000
      uptimeIntervalRef.current = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startedAtRef.current) / 1000)
        setStatus({ uptime: elapsed })
      }, 250)
    } else {
      if (uptimeIntervalRef.current) {
        clearInterval(uptimeIntervalRef.current)
        uptimeIntervalRef.current = null
      }
      if (status.state === 'stopped' || status.state === 'error') {
        startedAtRef.current = 0
        accumulatedUptimeRef.current = 0
        setStatus({ uptime: 0 })
      } else {
        accumulatedUptimeRef.current = status.uptime
      }
    }
    return () => {
      if (uptimeIntervalRef.current) {
        clearInterval(uptimeIntervalRef.current)
        uptimeIntervalRef.current = null
      }
    }
  }, [status.state])

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [logs, autoScroll])

  // Click outside to close listen mode dropdown
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement
      if (!target.closest('.bot-custom-select')) {
        setListenModeDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Preload all contacts for filter panels
  useEffect(() => {
    const loadContacts = async () => {
      try {
        const result = await window.electronAPI.chat.getContacts({ lite: true })
        if (result.success && result.contacts) {
          const mapped = (result.contacts as any[]).map((c: any) => ({
            username: c.username,
            displayName: c.displayName || c.username,
            avatarUrl: undefined as string | undefined,
            type: c.type || 'other',
          }))
          // Batch-fetch avatars from WCDB/head_image.db
          const usernames = mapped.map(c => c.username)
          const enrichResult = await window.electronAPI.chat.enrichSessionsContactInfo(usernames)
          if (enrichResult.success && enrichResult.contacts) {
            for (const c of mapped) {
              const info = enrichResult.contacts[c.username]
              if (info?.avatarUrl) {
                c.avatarUrl = info.avatarUrl
              }
            }
          }
          mapped.sort((a, b) => a.displayName.localeCompare(b.displayName, 'zh-CN'))
          setAllContacts(mapped)
        }
      } catch {}
    }
    loadContacts()
  }, [])

  const getContactTypeLabel = (type: string): string => {
    switch (type) {
      case 'friend': return '好友'
      case 'group': return '群聊'
      case 'official': return '公众号'
      default: return '其他'
    }
  }

  const getContactInfo = (username: string) => {
    return allContacts.find(c => c.username === username) || {
      username,
      displayName: username,
      avatarUrl: undefined,
      type: 'other' as string,
    }
  }

  const contactListKey = config.listenMode === 'blacklist' ? 'blacklist' : 'targetContacts'
  const contactList: string[] = (config as any)[contactListKey] || []

  const availableContacts = allContacts.filter(c => {
    if (contactList.includes(c.username)) return false
    const kw = botFilterSearchKeyword.trim().toLowerCase()
    if (kw) {
      return c.displayName.toLowerCase().includes(kw) || c.username.toLowerCase().includes(kw)
    }
    return true
  })

  const addContact = (username: string) => {
    if (contactList.includes(username)) return
    setConfig({ [contactListKey]: [...contactList, username] })
    setConfigDirty(true)
  }

  const removeContact = (username: string) => {
    setConfig({ [contactListKey]: contactList.filter((id: string) => id !== username) })
    setConfigDirty(true)
  }

  const addAllContacts = () => {
    const newList = [...new Set([...contactList, ...availableContacts.map(c => c.username)])]
    setConfig({ [contactListKey]: newList })
    setConfigDirty(true)
  }

  const removeAllContacts = () => {
    setConfig({ [contactListKey]: [] })
    setConfigDirty(true)
  }

  const handleStart = useCallback(async () => {
    const result = await window.electronAPI.bot.start()
    if (result.success) {
      setStatus({ state: 'running' })
    } else if (result.error) {
      addEvent({ type: 'error', message: result.error })
    }
  }, [addEvent])

  const handleStop = useCallback(async () => {
    await window.electronAPI.bot.stop()
    setStatus({ state: 'stopped', pythonRunning: false, wsConnected: false })
    clearLogs()
  }, [clearLogs])

  const handlePause = useCallback(async () => {
    await window.electronAPI.bot.pause()
  }, [])

  const handleResume = useCallback(async () => {
    await window.electronAPI.bot.resume()
  }, [])

  const handleSaveConfig = useCallback(async () => {
    // Persist all reply behavior config to backend
    const keys: Array<keyof BotConfig> = [
      'replyDelayMin', 'replyDelayMax', 'replyCooldown',
      'pollInterval', 'unreadScanInterval',
      'listenMode', 'targetContacts', 'blacklist',
    ]
    for (const key of keys) {
      await window.electronAPI.bot.setConfig(key, config[key])
    }
    setConfigDirty(false)
  }, [config])

  const handleClearLogs = useCallback(async () => {
    await window.electronAPI.bot.clearLogs()
    clearLogs()
  }, [])

  const isRunning = status.state === 'running'
  const isPaused = status.state === 'paused'
  const isStopped = status.state === 'stopped'
  const isError = status.state === 'error'

  return (
    <div className="bot-page">
      {/* ── Header ── */}
      <div className="bot-header">
        <div className="bot-header-left">
          <Bot size={24} />
          <h2>智能回复</h2>
        </div>
        <div className="bot-header-right">
          {/* Status badge */}
          <span className={`bot-status-badge ${isRunning ? 'running' : isPaused ? 'paused' : isError ? 'error' : 'stopped'}`}>
            {isRunning ? '运行中' : isPaused ? '已暂停' : isError ? '错误' : '已停止'}
          </span>
          {(isRunning || isPaused) && (
            <span className="bot-uptime">
              {isRunning ? '运行' : '已暂停'} {formatUptime(status.uptime)}
            </span>
          )}
          {status.wsConnected && (
            <span className="bot-ws-indicator" title="WebSocket 已连接">WS</span>
          )}
        </div>
      </div>

      {/* ── Controls ── */}
      <div className="bot-controls">
        {isStopped || isError ? (
          <button className="btn btn-primary bot-btn" onClick={handleStart}>
            <Play size={16} /> 启动
          </button>
        ) : (
          <button className="btn btn-danger bot-btn" onClick={handleStop}>
            <Square size={16} /> 停止
          </button>
        )}
        {isRunning && (
          <button className="btn btn-secondary bot-btn" onClick={handlePause}>
            <Pause size={16} /> 暂停
          </button>
        )}
        {isPaused && (
          <button className="btn btn-primary bot-btn" onClick={handleResume}>
            <Play size={16} /> 继续
          </button>
        )}
        <button className="btn btn-ghost bot-btn" onClick={handleClearLogs} disabled={logs.length === 0}>
          <Trash2 size={16} /> 清空日志
        </button>
      </div>

      {/* ── Stats bar ── */}
      <div className="bot-stats-bar">
        <div className="bot-stat">
          <MessageSquare size={14} />
          <span className="bot-stat-label">收到</span>
          <span className="bot-stat-value">{stats.messagesReceived}</span>
        </div>
        <div className="bot-stat">
          <Send size={14} />
          <span className="bot-stat-label">发送</span>
          <span className="bot-stat-value">{stats.repliesSent}</span>
        </div>
        <div className="bot-stat">
          <Zap size={14} />
          <span className="bot-stat-label">API</span>
          <span className="bot-stat-value">{stats.apiCalls}</span>
        </div>
        <div className="bot-stat">
          <AlertCircle size={14} />
          <span className="bot-stat-label">错误</span>
          <span className="bot-stat-value">{stats.errors}</span>
        </div>
        <div className="bot-stat">
          <RefreshCw size={14} />
          <span className="bot-stat-label">扫描</span>
          <span className="bot-stat-value">{stats.scanCount}</span>
        </div>
        <div className="bot-stat">
          <Clock size={14} />
          <span className="bot-stat-label">运行</span>
          <span className="bot-stat-value">{formatUptime(status.uptime)}</span>
        </div>
      </div>

      {/* ── Tabs ── */}
      <div className="bot-tabs">
        <button
          className={`bot-tab ${activeTab === 'log' ? 'active' : ''}`}
          onClick={() => setActiveTab('log')}
        >
          <Terminal size={14} /> 事件日志
        </button>
        <button
          className={`bot-tab ${activeTab === 'config' ? 'active' : ''}`}
          onClick={() => setActiveTab('config')}
        >
          <Settings size={14} /> 配置
        </button>
        <button
          className={`bot-tab ${activeTab === 'stats' ? 'active' : ''}`}
          onClick={() => setActiveTab('stats')}
        >
          <Zap size={14} /> 统计
        </button>
      </div>

      {/* ── Tab content ── */}
      <div className="bot-tab-content">
        {/* Log tab */}
        {activeTab === 'log' && (
          <div className="bot-log-panel">
            <div className="bot-log-controls">
              <label className="bot-autoscroll-label">
                <input
                  type="checkbox"
                  checked={autoScroll}
                  onChange={(e) => setAutoScroll(e.target.checked)}
                />
                自动滚动
              </label>
              <span className="bot-log-count">{logs.length} 条记录</span>
            </div>
            <div className="bot-log-list">
              {logs.length === 0 ? (
                <div className="bot-log-empty">
                  暂无事件，启动机器人后这里会显示实时日志
                </div>
              ) : (
                logs.map((event, i) => {
                  const meta = EVENT_ICONS[event.type] || { icon: '·', cls: '' }
                  return (
                    <div key={i} className={`bot-log-entry ${meta.cls}`}>
                      <span className="bot-log-time">{formatTime(event.ts)}</span>
                      <span className="bot-log-icon">{meta.icon}</span>
                      <span className="bot-log-label">{eventLabel(event)}</span>
                      {event.text && (
                        <span className="bot-log-text">{
                          event.text.length > 80
                            ? event.text.slice(0, 80) + '…'
                            : event.text
                        }</span>
                      )}
                    </div>
                  )
                })
              )}
              <div ref={logEndRef} />
            </div>
          </div>
        )}

        {/* Config tab */}
        {activeTab === 'config' && (
          <div className="bot-config-panel">
            <div className="bot-config-section-title">AI 模型配置</div>
            <div className="bot-config-shared-info">
              <div className="bot-config-readonly-field">
                <label className="bot-config-label">API 地址</label>
                <span className="bot-config-readonly-value">{config.baseUrl || '（未配置）'}</span>
              </div>
              <div className="bot-config-readonly-field">
                <label className="bot-config-label">模型</label>
                <span className="bot-config-readonly-value">{config.model || '（未配置）'}</span>
              </div>
              <div className="bot-config-readonly-field">
                <label className="bot-config-label">对话人格</label>
                <span className="bot-config-readonly-value">{config.persona || 'zhaoyoucai'}</span>
              </div>
              <button
                className="btn btn-secondary bot-config-nav-btn"
                onClick={() => navigate('/settings', { state: { initialTab: 'aiCommon' } })}
              >
                <Settings size={14} /> 在设置中修改
              </button>
              <p className="bot-config-hint">
                AI 模型配置与 AI 见解、群聊总结等功能共享。<br />
                在「设置 → AI → 基础配置」中统一管理。
              </p>
            </div>
            <div className="bot-config-section-title">回复行为</div>
            <div className="bot-config-row">
              <div className="bot-config-group">
                <label className="bot-config-label">回复延迟最小 (秒)</label>
                <input
                  type="number"
                  className="bot-config-input"
                  value={config.replyDelayMin}
                  onChange={(e) => { setConfig({ replyDelayMin: parseFloat(e.target.value) || 1.0 }); setConfigDirty(true) }}
                  step="0.5" min="0"
                />
              </div>
              <div className="bot-config-group">
                <label className="bot-config-label">回复延迟最大 (秒)</label>
                <input
                  type="number"
                  className="bot-config-input"
                  value={config.replyDelayMax}
                  onChange={(e) => { setConfig({ replyDelayMax: parseFloat(e.target.value) || 4.0 }); setConfigDirty(true) }}
                  step="0.5" min="0"
                />
              </div>
            </div>
            <div className="bot-config-group">
              <label className="bot-config-label">冷却时间 (秒)</label>
              <input
                type="number"
                className="bot-config-input"
                value={config.replyCooldown}
                onChange={(e) => { setConfig({ replyCooldown: parseFloat(e.target.value) || 12.0 }); setConfigDirty(true) }}
                step="1" min="0"
              />
            </div>
            <div className="bot-config-row">
              <div className="bot-config-group">
                <label className="bot-config-label">轮询间隔 (秒)</label>
                <input
                  type="number"
                  className="bot-config-input"
                  value={config.pollInterval}
                  onChange={(e) => { setConfig({ pollInterval: parseFloat(e.target.value) || 2.0 }); setConfigDirty(true) }}
                  step="0.5" min="1"
                />
              </div>
              <div className="bot-config-group">
                <label className="bot-config-label">扫描间隔 (秒)</label>
                <input
                  type="number"
                  className="bot-config-input"
                  value={config.unreadScanInterval}
                  onChange={(e) => { setConfig({ unreadScanInterval: parseFloat(e.target.value) || 3.0 }); setConfigDirty(true) }}
                  step="1" min="1"
                />
              </div>
            </div>

            <div className="bot-config-section-title">回复对象</div>
            <div className="bot-config-group">
              <label className="bot-config-label">回复模式</label>
              <div className="bot-custom-select">
                <div
                  className={`bot-custom-select-trigger ${listenModeDropdownOpen ? 'open' : ''}`}
                  onClick={() => setListenModeDropdownOpen(!listenModeDropdownOpen)}
                >
                  <span className="bot-custom-select-value">
                    {{ all: '全部联系人', specific: '仅指定联系人', blacklist: '排除指定联系人' }[config.listenMode]}
                  </span>
                  <ChevronDown size={14} className={`bot-custom-select-arrow ${listenModeDropdownOpen ? 'rotate' : ''}`} />
                </div>
                <div className={`bot-custom-select-dropdown ${listenModeDropdownOpen ? 'open' : ''}`}>
                  {[
                    { value: 'all' as const, label: '全部联系人', desc: '对所有消息进行智能回复' },
                    { value: 'specific' as const, label: '仅指定联系人', desc: '只回复列表中选中的联系人' },
                    { value: 'blacklist' as const, label: '排除指定联系人', desc: '回复所有人，但排除列表中的人' },
                  ].map(option => (
                    <div
                      key={option.value}
                      className={`bot-custom-select-option ${config.listenMode === option.value ? 'selected' : ''}`}
                      onClick={() => {
                        setConfig({ listenMode: option.value })
                        setConfigDirty(true)
                        setListenModeDropdownOpen(false)
                      }}
                    >
                      <div className="bot-custom-select-option-content">
                        <span className="bot-custom-select-option-label">{option.label}</span>
                        <span className="bot-custom-select-option-desc">{option.desc}</span>
                      </div>
                      {config.listenMode === option.value && <Check size={14} />}
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {config.listenMode !== 'all' && (
              <div className="bot-config-group">
                <label className="bot-config-label">
                  {config.listenMode === 'specific' ? '指定联系人' : '排除联系人'}
                </label>
                <span className="bot-config-group-hint">
                  {config.listenMode === 'specific'
                    ? '在左侧可选列表中添加需要回复的联系人'
                    : '在左侧可选列表中添加不需要回复的联系人'}
                </span>
                <div className="bot-contact-filter-container">
                  {/* 可选联系人 */}
                  <div className="bot-filter-panel">
                    <div className="bot-filter-panel-header">
                      <span>可选联系人</span>
                      {availableContacts.length > 0 && (
                        <button
                          type="button"
                          className="bot-filter-panel-action"
                          onClick={addAllContacts}
                        >
                          全选当前
                        </button>
                      )}
                      <div className="bot-filter-search-box">
                        <Search size={12} />
                        <input
                          type="text"
                          placeholder="搜索联系人..."
                          value={botFilterSearchKeyword}
                          onChange={(e) => setBotFilterSearchKeyword(e.target.value)}
                        />
                      </div>
                    </div>
                    <div className="bot-filter-panel-list">
                      {availableContacts.length > 0 ? (
                        availableContacts.map(c => (
                          <div
                            key={c.username}
                            className="bot-filter-panel-item"
                            onClick={() => addContact(c.username)}
                          >
                            <Avatar src={c.avatarUrl} name={c.displayName} size={28} />
                            <span className="bot-filter-item-name">{c.displayName}</span>
                            <span className="bot-filter-item-type">{getContactTypeLabel(c.type)}</span>
                            <span className="bot-filter-item-action">+</span>
                          </div>
                        ))
                      ) : (
                        <div className="bot-filter-panel-empty">
                          {botFilterSearchKeyword ? '没有匹配的联系人' : '暂无可添加的联系人'}
                        </div>
                      )}
                    </div>
                  </div>

                  {/* 已选联系人 */}
                  <div className="bot-filter-panel">
                    <div className="bot-filter-panel-header">
                      <span>{config.listenMode === 'specific' ? '已指定' : '已排除'}</span>
                      {contactList.length > 0 && (
                        <span className="bot-filter-panel-count">{contactList.length}</span>
                      )}
                      {contactList.length > 0 && (
                        <button
                          type="button"
                          className="bot-filter-panel-action"
                          onClick={removeAllContacts}
                        >
                          全部移除
                        </button>
                      )}
                    </div>
                    <div className="bot-filter-panel-list">
                      {contactList.length > 0 ? (
                        contactList.map(username => {
                          const info = getContactInfo(username)
                          return (
                            <div
                              key={username}
                              className="bot-filter-panel-item selected"
                              onClick={() => removeContact(username)}
                            >
                              <Avatar src={info.avatarUrl} name={info.displayName} size={28} />
                              <span className="bot-filter-item-name">{info.displayName}</span>
                              <span className="bot-filter-item-type">{getContactTypeLabel(info.type)}</span>
                              <span className="bot-filter-item-action">×</span>
                            </div>
                          )
                        })
                      ) : (
                        <div className="bot-filter-panel-empty">尚未添加任何联系人</div>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )}

            <button
              className="btn btn-primary bot-config-save"
              onClick={handleSaveConfig}
              disabled={!configDirty}
            >
              保存配置
            </button>
          </div>
        )}

        {/* Stats tab */}
        {activeTab === 'stats' && (
          <div className="bot-stats-panel">
            <div className="bot-stats-grid">
              <div className="bot-stat-card">
                <div className="bot-stat-card-icon messages"><MessageSquare size={24} /></div>
                <div className="bot-stat-card-value">{stats.messagesReceived}</div>
                <div className="bot-stat-card-label">收到消息</div>
              </div>
              <div className="bot-stat-card">
                <div className="bot-stat-card-icon replies"><Send size={24} /></div>
                <div className="bot-stat-card-value">{stats.repliesSent}</div>
                <div className="bot-stat-card-label">已发送回复</div>
              </div>
              <div className="bot-stat-card">
                <div className="bot-stat-card-icon api"><Zap size={24} /></div>
                <div className="bot-stat-card-value">{stats.apiCalls}</div>
                <div className="bot-stat-card-label">API 调用</div>
              </div>
              <div className="bot-stat-card">
                <div className="bot-stat-card-icon errors"><AlertCircle size={24} /></div>
                <div className="bot-stat-card-value">{stats.errors}</div>
                <div className="bot-stat-card-label">错误次数</div>
              </div>
              <div className="bot-stat-card">
                <div className="bot-stat-card-icon scans"><RefreshCw size={24} /></div>
                <div className="bot-stat-card-value">{stats.scanCount}</div>
                <div className="bot-stat-card-label">扫描次数</div>
              </div>
              <div className="bot-stat-card">
                <div className="bot-stat-card-icon uptime"><Clock size={24} /></div>
                <div className="bot-stat-card-value">{formatUptime(status.uptime)}</div>
                <div className="bot-stat-card-label">运行时间</div>
              </div>
            </div>
            <div className="bot-stats-detail">
              <h4>回复率</h4>
              <div className="bot-stats-rate">
                {stats.messagesReceived > 0
                  ? `${((stats.repliesSent / stats.messagesReceived) * 100).toFixed(1)}%`
                  : '—'}
              </div>
              <p className="bot-stats-hint">
                {stats.messagesReceived > 0
                  ? `${stats.repliesSent} / ${stats.messagesReceived} 条消息得到回复`
                  : '尚未收到消息'}
              </p>
            </div>
          </div>
        )}

      </div>
    </div>
  )
}
