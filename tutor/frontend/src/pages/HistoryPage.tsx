import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { SessionSummary } from '../domain/models'
import { tutorGateway } from '../services/gateway'

export function HistoryPage() {
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [error, setError] = useState('')
  useEffect(() => { void tutorGateway.getSessions().then(setSessions).catch((reason: Error) => setError(reason.message)) }, [])
  return <main className="history-page"><header><p className="eyebrow">Seu espaço</p><h1>Histórico de conversas</h1><p>Somente você e a equipe autorizada podem consultar estas conversas.</p></header>
    {error && <p role="alert" className="auth-error">{error}</p>}
    {!sessions.length && !error ? <section className="history-empty"><h2>Suas conversas aparecerão aqui</h2><p>Comece um chat para registrar uma conversa.</p><Link className="button" to="/chat">Iniciar conversa</Link></section> : <div className="history-cards">{sessions.map((session) => {
      return <Link className="history-card" to={`/chat?session=${encodeURIComponent(session.id)}`} key={session.id}><span className={`history-state ${session.status}`}>{session.status === 'open' ? 'Em andamento' : 'Encerrada'}</span><h2>{session.exercise.title}</h2><p>{session.last_student_message || 'Conversa iniciada'}</p><small>Atualizada {session.last_activity_at ? new Date(session.last_activity_at).toLocaleString() : ''}</small></Link>
    })}</div>}
  </main>
}
