import { ipcMain } from 'electron'
import { insightService } from '../services/insightService'
import { insightRecordService } from '../services/insightRecordService'
import { insightProfileService } from '../services/insightProfileService'
import { groupSummaryService } from '../services/groupSummaryService'

export function registerInsightHandlers(): void {
  // AI 见解
  ipcMain.handle('insight:testConnection', async () => {
    return insightService.testConnection()
  })

  ipcMain.handle('insight:getTodayStats', async () => {
    return insightService.getTodayStats()
  })

  ipcMain.handle('insight:listRecords', async (_, filters?: {
    keyword?: string
    sessionId?: string
    startTime?: number
    endTime?: number
    sourceType?: 'insight' | 'message_analysis' | 'all'
    limit?: number
    offset?: number
  }) => {
    return insightRecordService.listRecords(filters || {})
  })

  ipcMain.handle('insight:getRecord', async (_, id: string) => {
    return insightRecordService.getRecord(id)
  })

  ipcMain.handle('insight:markRecordRead', async (_, id: string) => {
    return insightRecordService.markRecordRead(id)
  })

  ipcMain.handle('insight:clearRecords', async (_, filters?: {
    sessionId?: string
    startTime?: number
    endTime?: number
  }) => {
    return insightRecordService.clearRecords(filters || {})
  })

  ipcMain.handle('insight:triggerTest', async () => {
    return insightService.triggerTest()
  })

  ipcMain.handle('insight:triggerSessionInsight', async (_, payload: {
    sessionId: string
    displayName?: string
    avatarUrl?: string
  }) => {
    return insightService.triggerSessionInsight(payload)
  })

  ipcMain.handle('insight:listProfileStatuses', async (_, sessionIds: string[]) => {
    return insightProfileService.listProfileStatuses(Array.isArray(sessionIds) ? sessionIds : [])
  })

  ipcMain.handle('insight:generateProfile', async (_, payload: {
    sessionId: string
    displayName?: string
    avatarUrl?: string
  }) => {
    return insightProfileService.generateProfile(payload)
  })

  ipcMain.handle('insight:cancelProfile', async (_, sessionId?: string) => {
    return insightProfileService.cancelProfile(sessionId)
  })

  ipcMain.handle('insight:generateFootprintInsight', async (_, payload: {
    rangeLabel: string
    summary: {
      private_inbound_people?: number
      private_replied_people?: number
      private_outbound_people?: number
      private_reply_rate?: number
      mention_count?: number
      mention_group_count?: number
    }
    privateSegments?: Array<{ displayName?: string; session_id?: string; incoming_count?: number; outgoing_count?: number; message_count?: number; replied?: boolean }>
    mentionGroups?: Array<{ displayName?: string; session_id?: string; count?: number }>
  }) => {
    return insightService.generateFootprintInsight(payload)
  })

  ipcMain.handle('insight:generateMessageInsight', async (_, payload: {
    sessionId: string
    displayName?: string
    avatarUrl?: string
    targetLocalId?: number
    targetCreateTime?: number
    targetMessageKey?: string
    targetText: string
    targetSenderName?: string
    contextCount?: number
    forceRefresh?: boolean
  }) => {
    return insightService.generateMessageInsight(payload)
  })

  // 群聊总结
  ipcMain.handle('groupSummary:listRecords', async (_, filters?: {
    sessionId?: string
    startTime?: number
    endTime?: number
    limit?: number
    offset?: number
  }) => {
    return groupSummaryService.listRecords(filters || {})
  })

  ipcMain.handle('groupSummary:getRecord', async (_, id: string) => {
    return groupSummaryService.getRecord(id)
  })

  ipcMain.handle('groupSummary:triggerManual', async (_, payload: {
    chatroomId: string
    displayName?: string
    avatarUrl?: string
    startTime: number
    endTime: number
  }) => {
    return groupSummaryService.triggerManual(payload)
  })

  ipcMain.handle('groupSummary:triggerDay', async (_, payload: {
    chatroomId: string
    displayName?: string
    avatarUrl?: string
    date: string
  }) => {
    return groupSummaryService.triggerDay(payload)
  })
}
