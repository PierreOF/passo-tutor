import { FormEvent, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/hooks'

export function ChangePasswordPage() {
  const { user, mustChangePassword, changePassword } = useAuth()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  if (!user) return <Navigate to="/login" replace />
  if (!mustChangePassword) return <Navigate to={user.role === 'admin' ? '/admin' : '/chat'} replace />

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')
    if (password.length < 6) { setError('Use pelo menos 6 caracteres.'); return }
    if (password !== confirmation) { setError('As senhas não coincidem.'); return }
    setSending(true)
    try {
      await changePassword(password)
      navigate('/login', { replace: true })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Não foi possível trocar a senha.')
    } finally {
      setSending(false)
    }
  }

  return <main className="auth-page"><form className="auth-card" onSubmit={submit}>
    <div className="auth-mark">passo<span>.</span></div>
    <p className="eyebrow">Primeiro acesso</p><h1>Crie sua senha</h1>
    <p>Troque a senha temporária antes de começar.</p>
    <label htmlFor="new-password">Nova senha</label>
    <input id="new-password" type="password" autoComplete="new-password" minLength={6} value={password} onChange={(event) => setPassword(event.target.value)} required />
    <label htmlFor="confirm-password">Confirme a senha</label>
    <input id="confirm-password" type="password" autoComplete="new-password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required />
    {error && <p className="auth-error" role="alert">{error}</p>}
    <button className="button auth-submit" type="submit" disabled={sending}>{sending ? 'Salvando…' : 'Salvar senha'}</button>
  </form></main>
}
