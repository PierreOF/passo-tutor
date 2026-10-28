import { ArrowRight, BookOpen, Braces, Bug, Code2, History, Plus } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ChatPromptComposer } from '../components/ChatPromptComposer'
import { ConversationMessages } from '../components/ConversationMessages'
import type { ChatJobReceipt, Session, SessionSummary } from '../domain/models'
import { tutorGateway } from '../services/gateway'

const suggestions = [
  { icon: Braces, label: 'Entender um conceito', text: 'Estou estudando laços de repetição e ainda não entendi quando usar for ou while.' },
  { icon: Bug, label: 'Investigar um erro', text: 'Meu código não produz o resultado esperado. Como posso começar a investigar o erro?' },
  { icon: Code2, label: 'Revisar uma tentativa', text: 'Tenho uma tentativa de código e quero ajuda para identificar o próximo passo.' },
]

export function ChatPage() {
  const [session, setSession] = useState<Session | null>(null)
  const [message, setMessage] = useState('')
  const [loading, setLoading] = useState(false)
  const [pendingText, setPendingText] = useState('')
  const [conversationHistory, setConversationHistory] = useState<SessionSummary[]>([])
  const [error, setError] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const receiptRef = useRef<ChatJobReceipt | null>(null)
  const stopRequestedRef = useRef(false)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const requestedId = searchParams.get('session')

  async function reloadHistory() { setConversationHistory((await tutorGateway.getSessions()).filter((item) => item.exercise.id === 'chat-livre' && Boolean(item.last_student_message))) }

  useEffect(() => {
    let mounted = true
    void tutorGateway.getSessions().then((history) => {
      if (mounted) setConversationHistory(history.filter((item) => item.exercise.id === 'chat-livre' && Boolean(item.last_student_message)))
    }).catch((reason) => { if (mounted) setError(reason instanceof Error ? reason.message : 'Não foi possível carregar o histórico.') })
    return () => { mounted = false }
  }, [])

  useEffect(() => {
    let mounted = true
    if (!requestedId) { setSession(null); return () => { mounted = false } }
    void tutorGateway.getSession(requestedId).then((found) => {
      if (mounted) {
        setSession(found.exerciseId === 'chat-livre' ? found : null)
        setLoading(found.exerciseId === 'chat-livre' && ['queued', 'running'].includes(found.generation?.status ?? ''))
      }
    }).catch((reason) => { if (mounted) setError(reason instanceof Error ? reason.message : 'Não foi possível abrir a conversa.') })
    return () => { mounted = false }
  }, [requestedId])

  useEffect(() => {
    if (!session?.id || !['queued', 'running'].includes(session.generation?.status ?? '')) return
    const sessionId = session.id
    let active = true
    const timer = window.setInterval(() => {
      void tutorGateway.getSession(sessionId).then((current) => {
        if (!active) return
        setSession(current)
        if (!['queued', 'running'].includes(current.generation?.status ?? '')) {
          setLoading(false)
          receiptRef.current = null
          setError(current.generation?.status === 'failed' ? current.generation.error || 'Não foi possível concluir a resposta.' : '')
          void reloadHistory().catch(() => {})
        }
      }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : 'Não foi possível acompanhar a resposta.') })
    }, 700)
    return () => { active = false; window.clearInterval(timer) }
  }, [session?.id, session?.generation?.jobId, session?.generation?.status])

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [session?.messages, loading])
  const sessionId = session?.id
  const sessionStatus = session?.status
  useEffect(() => {
    if (!sessionId || sessionStatus === 'closed') return
    void tutorGateway.heartbeat(sessionId)
    const timer = window.setInterval(() => { void tutorGateway.heartbeat(sessionId) }, 20_000)
    return () => window.clearInterval(timer)
  }, [sessionId, sessionStatus])

  const firstStudentMessage = session?.messages.find((item) => item.role === 'student')?.text
  const hasStarted = Boolean(firstStudentMessage)
  const showHistory = hasStarted || conversationHistory.length > 0
  const isOpeningState = !requestedId && !hasStarted && !loading
  const isClosed = session?.status === 'closed'

  async function send(text: string) {
    if (!text.trim() || loading || isClosed) return
    setError('')
    setPendingText(text)
    setMessage('')
    setLoading(true)
    stopRequestedRef.current = false
    let accepted: ChatJobReceipt | null = null
    try {
      const receipt = await tutorGateway.sendChatMessage(text, session?.id)
      accepted = receipt
      receiptRef.current = receipt
      const updated = await tutorGateway.getSession(receipt.sessionId)
      setSession(updated)
      setPendingText('')
      if (!session) navigate(`/chat?session=${encodeURIComponent(updated.id)}`, { replace: true })
      void reloadHistory().catch(() => setError('Não foi possível atualizar o histórico.'))
      if (!['queued', 'running'].includes(updated.generation?.status ?? '')) setLoading(false)
    } catch (reason) {
      setPendingText('')
      if (!accepted) setMessage(text)
      else if (!session) navigate(`/chat?session=${encodeURIComponent(accepted.sessionId)}`, { replace: true })
      setLoading(false)
      setError(reason instanceof Error ? reason.message : 'Não foi possível enviar a mensagem.')
    } finally {
      if (stopRequestedRef.current && receiptRef.current) void stopResponse(receiptRef.current)
    }
  }

  async function stopResponse(receipt = receiptRef.current ?? (session?.generation ? { sessionId: session.id, jobId: session.generation.jobId, messageId: session.generation.messageId } : null)) {
    stopRequestedRef.current = true
    if (!receipt) return
    try {
      const updated = await tutorGateway.stopChatResponse(receipt.sessionId, receipt.jobId)
      setSession(updated)
      setLoading(['queued', 'running'].includes(updated.generation?.status ?? ''))
      receiptRef.current = null
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Não foi possível parar a resposta.') }
  }

  async function continueResponse() {
    const generation = session?.generation
    if (!session || !generation || !['cancelled', 'failed'].includes(generation.status)) return
    setError('')
    setLoading(true)
    stopRequestedRef.current = false
    try {
      const receipt = await tutorGateway.continueChatResponse(session.id, generation.messageId)
      receiptRef.current = receipt
      const updated = await tutorGateway.getSession(session.id)
      setSession(updated)
      if (!['queued', 'running'].includes(updated.generation?.status ?? '')) setLoading(false)
    } catch (reason) { setLoading(false); setError(reason instanceof Error ? reason.message : 'Não foi possível continuar a resposta.') }
    finally { if (stopRequestedRef.current && receiptRef.current) void stopResponse(receiptRef.current) }
  }

  function startNewConversation() {
    setSession(null)
    setMessage('')
    setError('')
    navigate('/chat', { replace: true })
  }

  function openConversation(previousSession: SessionSummary) {
    setSession(null)
    setMessage('')
    setError('')
    navigate(`/chat?session=${encodeURIComponent(previousSession.id)}`, { replace: true })
  }

  return (
    <div className={`direct-chat-page${showHistory ? ' has-history' : ''}${hasStarted ? '' : ' is-empty'}`}>
      {showHistory && <aside className="chat-side history-side">
        <div className="history-heading"><span><History size={18} /> Conversas</span></div>
        <button className="new-chat-button" type="button" disabled={loading} onClick={startNewConversation}><Plus size={17} /> Nova conversa</button>
        <div className="history-group"><small>Histórico</small>
          {conversationHistory.map((item) => <button className={`history-item${item.id === session?.id ? ' active' : ''}`} type="button" disabled={loading} key={item.id} onClick={() => openConversation(item)}><span><b>{item.last_student_message}</b></span></button>)}
        </div>
        <Link className="side-exercise-link" to="/historico"><BookOpen size={18} /><span><small>Veja todas as atividades</small><b>Abrir histórico</b></span><ArrowRight size={17} /></Link>
      </aside>}

      <section className="direct-chat" aria-label="Conversa livre com o tutor">
        {error && <p className="auth-error" role="alert">{error}</p>}
        {isOpeningState && <p className="chat-opening-greeting">Olá! O que você está estudando hoje?</p>}
        <div className="direct-chat-content"><div className="direct-chat-list" aria-live="polite">
          {!loading && !hasStarted && (!requestedId || session) && <div className="suggestion-list">{suggestions.map(({ icon: Icon, label, text }) => <button key={label} type="button" onClick={() => setMessage(text)}><Icon size={17} /><span>{label}</span><ArrowRight size={15} /></button>)}</div>}
          <ConversationMessages messages={[
            ...(session?.messages ?? []),
            ...(pendingText ? [{ id: 'pending-send', role: 'student' as const, text: pendingText, pending: true }] : []),
          ]} preparing={loading}/>
          {isClosed && <p className="history-closed-note">Esta conversa atingiu o limite de mensagens. Você ainda pode consultar o histórico ou iniciar uma nova conversa.</p>}
          <div ref={endRef} />
        </div>
          <ChatPromptComposer disabled={loading || isClosed || Boolean(requestedId && !session)} responsePending={loading} canContinue={['cancelled', 'failed'].includes(session?.generation?.status ?? '')} onStop={() => { void stopResponse() }} onContinue={() => { void continueResponse() }} onSubmit={send} onValueChange={setMessage} value={message} />
        </div>
      </section>
    </div>
  )
}
