import type { ChatJobReceipt, Message, Session, SessionSummary, StudentState } from '../domain/models'
import type { TutorGateway } from './tutorGateway'

// Simulador local do chat (sem backend). Reproduz só o formato da conversa: o tutor responde depois
// do aluno, cada sondagem tem até dez turnos e encerrar uma sondagem não fecha a conversa.
const PROBE_TURNS = 10
const wait = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))
const uid = () => crypto.randomUUID?.() ?? Math.random().toString(36).slice(2)

const emptyState = (): StudentState => ({
  conceptInDoubt: 'não identificado', difficultyType: 'não identificado', helpLevel: 1, directSolutionRequests: 0,
  allowedExposureLevel: 1, shortHistory: [], countableTurns: 0, mode: 'PROBING', status: 'continuar',
})

const asksForSolution = (text: string) => /solu[cç][aã]o|c[oó]digo (pronto|completo)|fun[cç][aã]o (pronta|inteira)|fa[cç]a.*para mim/i.test(text)
const isInjection = (text: string) => /ignore.*instru|prompt (do sistema|secreto)|racioc[ií]nio interno/i.test(text)

function reply(text: string, closing: boolean): { text: string; kind: Message['kind'] } {
  if (closing) return { text: 'O conceito central desta dúvida ficou registrado; a lacuna permanece pendente para revisão.', kind: 'question' }
  if (isInjection(text)) return { text: 'Não posso mostrar instruções internas. Qual parte da sua dúvida você consegue descrever com suas palavras?', kind: 'fallback' }
  if (asksForSolution(text)) return { text: 'Eu não vou montar a solução por você. O que você já tentou, mesmo que esteja incompleto?', kind: 'fallback' }
  return { text: 'Como explicaria esse problema com suas próprias palavras, sem pensar ainda na sintaxe?', kind: 'question' }
}

export class MockTutorGateway implements TutorGateway {
  private sessions = new Map<string, Session>()
  private timers = new Map<string, ReturnType<typeof setTimeout>>()

  async sendChatMessage(text: string, sessionId?: string): Promise<ChatJobReceipt> {
    const trimmed = text.trim()
    if (!trimmed || trimmed.length > 8000) throw new Error('Mensagem inválida para o chat livre.')
    const now = new Date().toISOString()
    const base: Session = sessionId ? await this.getSession(sessionId)
      : { id: uid(), exerciseId: 'chat-livre', messages: [], state: emptyState(), status: 'open', createdAt: now, lastActivityAt: now, probeCount: 0 }
    if (base.status === 'closed') throw new Error('Esta conversa está encerrada.')
    if (base.generation?.status === 'queued' || base.generation?.status === 'running') throw new Error('Aguarde a resposta anterior.')
    const messageId = uid()
    const jobId = uid()
    this.sessions.set(base.id, { ...base, messages: [...base.messages, { id: messageId, role: 'student', text: trimmed, createdAt: now }], generation: { jobId, messageId, status: 'running' }, lastActivityAt: now })
    this.schedule(base.id, messageId, jobId)
    return { sessionId: base.id, messageId, jobId }
  }

  async stopChatResponse(sessionId: string, jobId: string): Promise<Session> {
    const session = await this.getSession(sessionId)
    if (session.generation?.jobId === jobId && session.generation.status === 'running') {
      clearTimeout(this.timers.get(jobId))
      this.timers.delete(jobId)
      const stopped = { ...session, generation: { ...session.generation, status: 'cancelled' as const } }
      this.sessions.set(sessionId, stopped)
      return stopped
    }
    return session
  }

  async continueChatResponse(sessionId: string, messageId: string): Promise<ChatJobReceipt> {
    const session = await this.getSession(sessionId)
    if (session.generation?.messageId !== messageId || !['cancelled', 'failed'].includes(session.generation.status)) throw new Error('Mensagem não disponível para continuar.')
    const jobId = uid()
    this.sessions.set(sessionId, { ...session, generation: { jobId, messageId, status: 'running' } })
    this.schedule(sessionId, messageId, jobId)
    return { sessionId, messageId, jobId }
  }

  private schedule(sessionId: string, messageId: string, jobId: string) {
    const timer = setTimeout(() => {
      this.timers.delete(jobId)
      void wait(650).then(() => {
        const current = this.sessions.get(sessionId)
        if (!current || current.generation?.jobId !== jobId || current.generation.status !== 'running') return
        const student = current.messages.find((item) => item.id === messageId)
        if (!student) return
        // Sondagem encerrada: a próxima fala abre uma nova (novo contrato N0 no backend real).
        const startsProbe = current.state.mode !== 'PROBING'
        const probeIndex = (current.probeCount ?? 0) - (startsProbe || !current.probeCount ? 0 : 1)
        const previous = startsProbe ? emptyState() : current.state
        const turns = (previous.countableTurns ?? 0) + 1
        const closing = turns >= PROBE_TURNS
        const answer = reply(student.text, closing)
        const now = new Date().toISOString()
        const state: StudentState = {
          ...previous, countableTurns: turns, shortHistory: [...previous.shortHistory, student.text].slice(-6),
          directSolutionRequests: previous.directSolutionRequests + Number(asksForSolution(student.text)),
          mode: closing ? 'CLOSING_LIMIT' : 'PROBING', status: closing ? 'encerrada_por_limite' : 'continuar',
        }
        const messages = current.messages.map((item) => item.id === messageId ? { ...item, probeIndex } : item)
        this.sessions.set(sessionId, {
          ...current, state, probeCount: probeIndex + 1, lastActivityAt: now,
          messages: [...messages, { id: uid(), role: 'tutor', text: answer.text, kind: answer.kind, createdAt: now, probeIndex }],
          generation: { jobId, messageId, status: 'completed' },
        })
      })
    }, 0)
    this.timers.set(jobId, timer)
  }

  async getSessions(): Promise<SessionSummary[]> {
    return [...this.sessions.values()].map((session) => ({ id: session.id, status: session.status ?? 'open', exercise: { id: session.exerciseId, title: 'Conversa livre' }, last_student_message: [...session.messages].reverse().find((message) => message.role === 'student')?.text ?? '', last_activity_at: session.lastActivityAt, online: true }))
  }

  async getSession(sessionId: string): Promise<Session> {
    const session = this.sessions.get(sessionId)
    if (!session) throw new Error('Conversa não encontrada.')
    return session
  }

  async heartbeat(): Promise<void> {}

  async closeSession(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId)
    if (session) this.sessions.set(sessionId, { ...session, status: 'closed' })
  }
}
