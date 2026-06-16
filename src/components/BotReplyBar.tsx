import { useState, useCallback } from 'react'
import { Send, Loader2 } from 'lucide-react'
import './BotReplyBar.scss'

interface BotReplyBarProps {
  contactName: string
  isBotConnected: boolean
}

function estimateTypingDuration(text: string): number {
  let total = 0
  for (const ch of text) {
    const cp = ch.codePointAt(0) || 0
    const isCJK = (cp >= 0x4E00 && cp <= 0x9FFF) || (cp >= 0x3400 && cp <= 0x4DBF)
    const base = isCJK ? 115 : 62
    total += base
  }
  const sentenceCount = (text.match(/[。！？!?.\n]/g) || []).length
  total += sentenceCount * 500
  total += 350
  return Math.max(0.5, Math.round(total / 100) / 10)
}

export default function BotReplyBar({ contactName, isBotConnected }: BotReplyBarProps) {
  const [message, setMessage] = useState('')
  const [sendState, setSendState] = useState<'idle' | 'sending' | 'sent' | 'error'>('idle')
  const [sendError, setSendError] = useState('')

  const handleSend = useCallback(async () => {
    const trimmed = message.trim()
    if (!trimmed || sendState === 'sending') return

    setSendState('sending')
    setSendError('')
    try {
      const result = await window.electronAPI.bot.sendMessage(contactName, trimmed)
      if (result.success) {
        setSendState('sent')
        setMessage('')
        setTimeout(() => setSendState('idle'), 2500)
      } else {
        setSendState('error')
        setSendError(result.error || '发送失败')
      }
    } catch (err: any) {
      setSendState('error')
      setSendError(err.message || '发送失败')
    }
  }, [message, contactName, sendState])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.ctrlKey && e.key === 'Enter') {
      e.preventDefault()
      e.stopPropagation()
      ;(e.target as HTMLElement).blur()
      handleSend()
    }
  }, [handleSend])

  const charCount = message.length
  const estDuration = message.trim() ? estimateTypingDuration(message.trim()) : 0
  const canSend = message.trim().length > 0 && sendState !== 'sending'

  return (
    <div className="bot-reply-bar">
      <div className="bot-reply-bar-body">
        <textarea
          className="bot-reply-bar-textarea"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            isBotConnected
              ? '输入消息... (Ctrl+Enter 发送)'
              : '输入消息... (Ctrl+Enter 直接发送到微信窗口)'
          }
          rows={2}
          maxLength={2000}
          disabled={sendState === 'sending'}
        />
        <button
          className="bot-reply-send-btn"
          disabled={!canSend}
          onClick={handleSend}
          title={isBotConnected ? 'Ctrl+Enter 发送 (机器人模式)' : 'Ctrl+Enter 发送 (直接粘贴到微信)'}
        >
          {sendState === 'sending' ? (
            <Loader2 size={16} className="spin" />
          ) : (
            <Send size={16} />
          )}
        </button>
      </div>
      <div className="bot-reply-bar-footer">
        <span className="bot-reply-bar-charcount">{charCount}/2000</span>
        {message.trim() && (
          <span className="bot-reply-bar-estimate">
            {isBotConnected ? `预计 ${estDuration}秒` : '直接发送'}
          </span>
        )}
        {sendState === 'sent' && (
          <span className="bot-reply-bar-status sent">已发送</span>
        )}
        {sendState === 'error' && (
          <span className="bot-reply-bar-status error">{sendError}</span>
        )}
        {sendState === 'sending' && (
          <span className="bot-reply-bar-status sending">正在输入...</span>
        )}
      </div>
    </div>
  )
}
