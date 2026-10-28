import type { ChatJobReceipt, Session, SessionSummary } from '../domain/models'
import type { TutorGateway } from './tutorGateway'
import { request as httpRequest } from './http'

const request = <T>(url: string, init?: RequestInit) => httpRequest<T>(url, init, { fallbackMessage: 'A API local não concluiu a solicitação.', includeRequestId: true })

export class HttpTutorGateway implements TutorGateway {
  sendChatMessage(text: string, sessionId?: string): Promise<ChatJobReceipt> {
    return request<ChatJobReceipt>(sessionId ? `/api/tutor/chat/${encodeURIComponent(sessionId)}/messages` : '/api/tutor/chat', {
      method: 'POST', body: JSON.stringify({ studentMessage: text }),
    })
  }

  stopChatResponse(sessionId: string, jobId: string): Promise<Session> {
    return request<Session>(`/api/tutor/chat/${encodeURIComponent(sessionId)}/responses/${encodeURIComponent(jobId)}/stop`, { method: 'POST', body: '{}' })
  }

  continueChatResponse(sessionId: string, messageId: string): Promise<ChatJobReceipt> {
    return request<ChatJobReceipt>(`/api/tutor/chat/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/continue`, { method: 'POST', body: '{}' })
  }
  getSessions(): Promise<SessionSummary[]> {
    return request<SessionSummary[]>('/api/tutor/sessions')
  }

  getSession(sessionId: string): Promise<Session> {
    return request<Session>(`/api/tutor/sessions/${encodeURIComponent(sessionId)}`)
  }

  async heartbeat(sessionId: string): Promise<void> {
    await request<{ ok: boolean }>(`/api/tutor/sessions/${encodeURIComponent(sessionId)}/heartbeat`)
  }

  async closeSession(sessionId: string): Promise<void> {
    await request<{ ok: boolean }>(`/api/tutor/sessions/${encodeURIComponent(sessionId)}/close`, { method: 'POST', body: '{}' })
  }
}
