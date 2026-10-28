import { request as httpRequest } from './http'
import type { SessionSummary } from '../domain/models'

export interface AdminStudent {
  id: string
  student_code: string
  active: number | boolean
  must_change_password: number | boolean
  created_at: string
  conversationCount: number
}

export interface AdminConversation extends SessionSummary {
  user_id: string
  messages?: Array<{ id: string; role: string; text: string; kind?: string; code_snapshot?: string; execution_result?: Record<string, unknown>; created_at?: string }>
  activities?: Array<{ id: string; kind: string; code_snapshot: string; result: Record<string, unknown>; created_at: string }>
}

const request = <T>(path: string) => httpRequest<T>(path, undefined, { fallbackMessage: 'Não foi possível carregar os dados administrativos.' })

export const adminApi = {
  students: () => request<AdminStudent[]>('/api/admin/students'),
  conversations: (query: URLSearchParams) => request<AdminConversation[]>(`/api/admin/conversations?${query}`),
  conversation: (id: string) => request<AdminConversation>(`/api/admin/conversations/${encodeURIComponent(id)}`),
}
