import { FormEvent, useState } from 'react'
import { Navigate, Link, useLocation } from 'react-router-dom'
import { useAuth } from '../auth/hooks'

export function LoginPage() {
  const { user, login, mustChangePassword } = useAuth()
  const location = useLocation()
  const [studentCode, setStudentCode] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const requested = typeof location.state?.from === 'string' ? location.state.from : ''
  const allowed = user?.role === 'admin' ? /^\/admin(?:[/?]|$)/ : /^\/(?:chat|historico)(?:[/?]|$)/
  if (user) return <Navigate to={mustChangePassword ? '/alterar-senha' : allowed.test(requested) ? requested : user.role === 'admin' ? '/admin' : '/chat'} replace />

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')
    setSending(true)
    try {
      await login(studentCode, password)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Não foi possível entrar.')
    } finally {
      setSending(false)
    }
  }

  return <main className="auth-page">
    <form className="auth-card" onSubmit={submit}>
      <div className="auth-mark">passo<span>.</span></div>
      <Link className="auth-back" to="/">← Voltar à página inicial</Link>
      <p className="eyebrow">Acesso à plataforma</p>
      <h1>Entre na sua conta</h1>
      <p>Use o código de aluno e a senha fornecidos pela equipe.</p>
      <label htmlFor="student-code">Código de aluno</label>
      <input id="student-code" autoComplete="username" value={studentCode} onChange={(event) => setStudentCode(event.target.value)} required />
      <label htmlFor="password">Senha</label>
      <input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
      {error && <p className="auth-error" role="alert">{error}</p>}
      <button className="button auth-submit" type="submit" disabled={sending}>{sending ? 'Entrando…' : 'Entrar'}</button>
      <small>As conversas, os códigos enviados e os resultados de execução ficam salvos e podem ser consultados pela equipe da plataforma.</small>
    </form>
  </main>
}
