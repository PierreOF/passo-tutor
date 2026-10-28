import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { adminApi, type AdminConversation, type AdminStudent } from '../services/adminApi'

/** Repete `tick` a cada 5 s enquanto a aba estiver visível; ao voltar a ficar visível, atualiza na hora. */
function pollWhileVisible(tick: () => void): () => void {
  const timer = window.setInterval(() => { if (!document.hidden) tick() }, 5000)
  const onVisibility = () => { if (!document.hidden) tick() }
  document.addEventListener('visibilitychange', onVisibility)
  return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisibility) }
}

/** Conversas reais do chat, somente leitura. As contas são criadas pelo CLI (`create-student`), não pelo painel. */
export function AdminRecordsPage() {
  const [students, setStudents] = useState<AdminStudent[]>([])
  const [conversations, setConversations] = useState<AdminConversation[]>([])
  const [selectedStudent, setSelectedStudent] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [selected, setSelected] = useState<AdminConversation | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const params = new URLSearchParams()
      if (selectedStudent) params.set('student_id', selectedStudent)
      if (filterStatus) params.set('status', filterStatus)
      const [nextStudents, nextConversations] = await Promise.all([adminApi.students(), adminApi.conversations(params)])
      setStudents(nextStudents)
      setConversations(nextConversations)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Não foi possível atualizar o painel.')
    } finally { setLoading(false) }
  }, [selectedStudent, filterStatus])

  useEffect(() => {
    void refresh()
    return pollWhileVisible(() => { void refresh() })
  }, [refresh])

  const selectedId = selected?.id
  useEffect(() => {
    if (!selectedId) return
    let active = true
    const stop = pollWhileVisible(() => { void adminApi.conversation(selectedId).then(value => { if (active) setSelected(value) }).catch(() => { if (active) setError('Não foi possível atualizar a conversa.') }) })
    return () => { active = false; stop() }
  }, [selectedId])

  const transcript = selected ? [
    ...(selected.messages ?? []),
    ...(selected.activities ?? []).map((activity) => ({ id: activity.id, role: 'activity', text: 'Execução de código', code_snapshot: activity.code_snapshot, execution_result: activity.result, created_at: activity.created_at })),
  ].sort((left, right) => (left.created_at ?? '').localeCompare(right.created_at ?? '')) : []

  return <section className="admin-page admin-records">
    <Link className="review-back" to="/admin"><ArrowLeft size={16}/> Voltar ao painel</Link>
    <h1>Conversas do chat</h1>
    {error && <p className="admin-error" role="alert">{error}</p>}
    <div className="admin-grid records-grid">
      <section className="admin-panel"><div className="admin-panel-heading"><div><p className="eyebrow">Acompanhamento</p><h2>Conversas</h2></div><span>{conversations.length} registros</span></div>
        <div className="admin-filters"><label>Aluno<select value={selectedStudent} onChange={(event) => setSelectedStudent(event.target.value)}><option value="">Todos</option>{students.map((student) => <option key={student.id} value={student.id}>{student.student_code}</option>)}</select></label><label>Estado<select value={filterStatus} onChange={(event) => setFilterStatus(event.target.value)}><option value="">Todos</option><option value="open">Em andamento</option><option value="closed">Encerrados</option></select></label></div>
        {loading ? <p>Carregando conversas…</p> : <div className="admin-conversation-list">{conversations.map((conversation) => <button type="button" className={`admin-conversation${selected?.id === conversation.id ? ' selected' : ''}`} key={conversation.id} onClick={() => void adminApi.conversation(conversation.id).then(setSelected).catch((reason: Error) => setError(reason.message))}>
          <span><strong>{conversation.student_code}</strong><small>{conversation.exercise.title}</small><small>{conversation.last_student_message || 'Conversa iniciada'}</small></span><span className="admin-conversation-status"><i className={conversation.online ? 'online' : ''} />{conversation.online ? 'Online' : conversation.status === 'open' ? 'Em andamento' : 'Encerrada'}</span>
        </button>)}{!conversations.length && <p className="admin-empty">Nenhuma conversa para estes filtros.</p>}</div>}
      </section>
    </div>
    {selected && <section className="admin-panel admin-transcript"><div className="admin-panel-heading"><div><p className="eyebrow">{selected.student_code} · {selected.exercise.title}</p><h2>Histórico da conversa</h2></div><button className="secondary-button" type="button" onClick={() => setSelected(null)}>Fechar</button></div>
      <div className="admin-transcript-list">{transcript.map((message) => <article className={`admin-transcript-message ${message.role}`} key={message.id}><small>{message.role === 'student' ? selected.student_code : message.role === 'activity' ? 'Atividade' : 'Tutor'} · {message.created_at ? new Date(message.created_at).toLocaleString() : ''}</small><p>{message.text}</p>{message.code_snapshot && <details><summary>Código enviado</summary><pre>{message.code_snapshot}</pre></details>}{message.execution_result && <details><summary>Resultado da execução</summary><pre>{JSON.stringify(message.execution_result, null, 2)}</pre></details>}</article>)}</div>
    </section>}
  </section>
}
