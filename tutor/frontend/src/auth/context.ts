import { createContext } from 'react'
import type { AuthUser } from '../services/authApi'

interface AuthContextValue {
  user: AuthUser | null
  loading: boolean
  mustChangePassword: boolean
  login: (studentCode: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  changePassword: (password: string) => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)
