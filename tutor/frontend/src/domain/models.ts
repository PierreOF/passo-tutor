type ExposureLevel = 1 | 2 | 3 | 4 | 5

export interface StudentState {
  conceptInDoubt: string
  difficultyType: string
  helpLevel: number
  directSolutionRequests: number
  allowedExposureLevel: ExposureLevel
  shortHistory: string[]
  mode?: 'PROBING' | 'CLOSING_RESOLVED' | 'CLOSING_LIMIT'
  status?: 'continuar' | 'encerrada_resolvida' | 'encerrada_por_limite'
  countableTurns?: number
  gapResolved?: boolean
}

export interface Message {
  id: string
  role: 'student' | 'tutor'
  text: string
  kind?: 'question' | 'hint' | 'pseudocode' | 'partial' | 'fallback' | 'out_of_scope'
  createdAt: string
  /** Sondagem a que a mensagem pertence; muda quando o aluno traz uma nova dúvida. */
  probeIndex?: number
}

export interface Session {
  id: string
  exerciseId: string
  trackId?: string
  messages: Message[]
  state: StudentState
  code?: string
  probeCount?: number
  status?: 'open' | 'closed'
  createdAt?: string
  lastActivityAt?: string
  generation?: { jobId: string; messageId: string; status: 'queued' | 'running' | 'completed' | 'cancelled' | 'failed'; error?: string }
}

export interface ChatJobReceipt { sessionId: string; jobId: string; messageId: string }

export interface SessionSummary {
  id: string
  student_code?: string
  status: 'open' | 'closed'
  exercise: { id: string; title: string; track_id?: string; trackId?: string }
  created_at?: string
  last_activity_at?: string
  heartbeat_at?: string | null
  last_student_message: string
  online?: boolean
}
