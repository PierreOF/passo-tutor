import { request as httpRequest } from './http'

export interface AuthUser {
  id: string
  studentCode: string
  role: 'admin' | 'student'
  active: boolean
}

export interface AuthState {
  user: AuthUser | null
  mustChangePassword: boolean
}

const request = <T>(path: string, init?: RequestInit) => httpRequest<T>(path, init, { fallbackMessage: 'A solicitação não foi concluída.' })

export const authApi = {
  login(studentCode: string, password: string) {
    return request<AuthState>('/api/auth/login', { method: 'POST', body: JSON.stringify({ studentCode, password }) })
  },
  async me(): Promise<AuthState> {
    return request<AuthState>('/api/auth/me')
  },
  async logout(): Promise<void> {
    await request('/api/auth/logout', { method: 'POST', body: '{}' })
  },
  async changePassword(password: string): Promise<void> {
    await request('/api/auth/change-password', { method: 'POST', body: JSON.stringify({ password }) })
  },
}
