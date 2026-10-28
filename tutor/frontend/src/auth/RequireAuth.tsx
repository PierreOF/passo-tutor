import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from './hooks'

export function RequireAuth({ role }: { role?: 'admin' | 'student' }) {
  const { user, loading, mustChangePassword } = useAuth()
  const location = useLocation()
  if (loading) return <div className="auth-loading" role="status">Verificando acesso…</div>
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  if (mustChangePassword && location.pathname !== '/alterar-senha') return <Navigate to="/alterar-senha" replace />
  if (role && user.role !== role) return <Navigate to={user.role === 'admin' ? '/admin' : '/chat'} replace />
  return <Outlet />
}
