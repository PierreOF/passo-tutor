import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Session } from '../domain/models'
import { HttpTutorGateway } from './httpTutorGateway'

const session: Session = {
  id: 'session-1', exerciseId: 'chat-livre', messages: [],
  state: { conceptInDoubt: 'paridade', difficultyType: 'lógica', helpLevel: 1, directSolutionRequests: 0, allowedExposureLevel: 1, shortHistory: [] },
}

describe('HttpTutorGateway', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('propaga mensagem pública de erro sem expor resposta bruta', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ message: 'Configure o backend.' }), { status: 503 })))
    await expect(new HttpTutorGateway().getSession('session-1')).rejects.toThrow('Configure o backend.')
  })

  it('envia, interrompe e continua respostas do chat livre', async () => {
    const receipt = { sessionId: 'chat-1', jobId: 'job-1', messageId: 'message-1' }
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(receipt), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...session, id: 'chat-1' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(receipt), { status: 202 }))
    vi.stubGlobal('fetch', fetchMock)
    const gateway = new HttpTutorGateway()
    expect(await gateway.sendChatMessage('Minha dúvida')).toEqual(receipt)
    await gateway.stopChatResponse('chat-1', 'job-1')
    await gateway.continueChatResponse('chat-1', 'message-1')
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/tutor/chat', '/api/tutor/chat/chat-1/responses/job-1/stop', '/api/tutor/chat/chat-1/messages/message-1/continue',
    ])
    // O tutor não recebe exercício: a primeira mensagem leva só o texto do aluno.
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ studentMessage: 'Minha dúvida' })
  })
})
