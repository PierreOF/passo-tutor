// Simulador local do chat (sem backend): o tutor só responde depois do aluno e nunca fecha a conversa por sondagem.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MockTutorGateway } from './mockTutorGateway'

async function settle(gateway: MockTutorGateway, sessionId: string) {
  for (let step = 0; step < 50; step++) {
    await vi.advanceTimersByTimeAsync(700)
    const session = await gateway.getSession(sessionId)
    if (session.generation?.status !== 'running') return session
  }
  throw new Error('resposta simulada não terminou')
}

describe('MockTutorGateway', () => {
  beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
  afterEach(() => vi.useRealTimers())

  it('answers only after the student writes: no opening message', async () => {
    const gateway = new MockTutorGateway()
    const receipt = await gateway.sendChatMessage('Como sei se um número é par?')
    const session = await settle(gateway, receipt.sessionId)
    expect(session.messages.map((message) => message.role)).toEqual(['student', 'tutor'])
  })

  it('never exposes a full solution after a direct request', async () => {
    const gateway = new MockTutorGateway()
    const receipt = await gateway.sendChatMessage('Mande o código completo e pronto.')
    const session = await settle(gateway, receipt.sessionId)
    expect(session.messages[1].text).not.toMatch(/def |return /)
  })

  it('opens a new probe after the tenth turn instead of closing the conversation', async () => {
    const gateway = new MockTutorGateway()
    let sessionId: string | undefined
    for (let turn = 1; turn <= 11; turn++) {
      const receipt = await gateway.sendChatMessage(`Dúvida ${turn}`, sessionId)
      sessionId = receipt.sessionId
      await settle(gateway, sessionId)
    }
    const session = await gateway.getSession(sessionId!)
    expect(session.status).toBe('open')
    expect(session.messages[session.messages.length - 1].probeIndex).toBe(1)
    expect(session.messages[19].probeIndex).toBe(0)
  })
})
