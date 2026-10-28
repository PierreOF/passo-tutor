import type { ChatJobReceipt, Session, SessionSummary } from '../domain/models'

/** Somente o chat com o tutor: a conversa é uma sequência de sondagens; o aluno escreve o código no próprio texto. */
export interface TutorGateway {
  sendChatMessage(text: string, sessionId?: string): Promise<ChatJobReceipt>
  stopChatResponse(sessionId: string, jobId: string): Promise<Session>
  continueChatResponse(sessionId: string, messageId: string): Promise<ChatJobReceipt>
  getSessions(): Promise<SessionSummary[]>
  getSession(sessionId: string): Promise<Session>
  heartbeat(sessionId: string): Promise<void>
  closeSession(sessionId: string): Promise<void>
}
