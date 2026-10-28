import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { AuthContext } from './context'
import { authApi, type AuthState } from '../services/authApi'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ user: null, mustChangePassword: false })
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setState(await authApi.me())
    } catch {
      setState({ user: null, mustChangePassword: false })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const login = useCallback(async (studentCode: string, password: string) => {
    setState(await authApi.login(studentCode, password))
  }, [])

  const logout = useCallback(async () => {
    try { await authApi.logout() } finally { setState({ user: null, mustChangePassword: false }) }
  }, [])

  const changePassword = useCallback(async (password: string) => {
    await authApi.changePassword(password)
    setState({ user: null, mustChangePassword: false })
  }, [])

  const value = useMemo(() => ({ user: state.user, loading, mustChangePassword: state.mustChangePassword, login, logout, refresh, changePassword }), [state, loading, login, logout, refresh, changePassword])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
