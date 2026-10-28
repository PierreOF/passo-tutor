import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ChatPage } from './ChatPage'
import { tutorGateway } from '../services/gateway'
import type { Session } from '../domain/models'

describe('ChatPage progressive layout', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('reveals conversation history only after the first message', async () => {
    const start = vi.spyOn(tutorGateway, 'sendChatMessage')
    render(<MemoryRouter><ChatPage /></MemoryRouter>)

    const suggestion = await screen.findByRole('button', { name: /Entender um conceito/ })
    expect(screen.getByRole('textbox', { name: 'Sua mensagem para o tutor' })).toBeEnabled()
    expect(start).not.toHaveBeenCalled()
    expect(screen.queryByText('Conversas')).not.toBeInTheDocument()

    fireEvent.click(suggestion)
    fireEvent.click(screen.getByRole('button', { name: 'Enviar mensagem' }))
    await screen.findByText(/Como explicaria esse problema/, {}, { timeout: 4000 })
    expect(start).toHaveBeenCalledTimes(1)

    expect(await screen.findByText('Conversas')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Estou estudando laços de repetição/ })).toBeInTheDocument()
    expect(await screen.findByText(/Como explicaria esse problema/)).toBeInTheDocument()
  })

  it('offers only text input, without attachment, model settings or voice controls', async () => {
    render(<MemoryRouter><ChatPage /></MemoryRouter>)

    expect(await screen.findByRole('textbox', { name: 'Sua mensagem para o tutor' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Anexar imagem' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Configurações do modelo selecionado' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Entrada por voz/ })).not.toBeInTheDocument()
  })

  it('keeps the draft after the first request fails without retrying', async () => {
    const start = vi.spyOn(tutorGateway, 'sendChatMessage').mockRejectedValue(new Error('Falha de teste'))
    render(<MemoryRouter><ChatPage /></MemoryRouter>)
    const input = screen.getByRole('textbox', { name: 'Sua mensagem para o tutor' })
    fireEvent.change(input, { target: { value: 'Minha dúvida' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enviar mensagem' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Falha de teste')
    expect(input).toHaveValue('Minha dúvida')
    expect(input).toBeEnabled()
    expect(start).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/Preparando uma pergunta/)).not.toBeInTheDocument()
  })

  it('reopens an earlier conversation from the history', async () => {
    const close = vi.spyOn(tutorGateway, 'closeSession')
    render(<MemoryRouter><ChatPage /></MemoryRouter>)

    const suggestion = await screen.findByRole('button', { name: /Entender um conceito/ })
    fireEvent.click(suggestion)
    fireEvent.click(screen.getByRole('button', { name: 'Enviar mensagem' }))
    await screen.findByRole('button', { name: /Estou estudando laços de repetição/ })

    await waitFor(() => expect(screen.getByRole('button', { name: 'Nova conversa' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Nova conversa' }))
    expect(close).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Sua mensagem para o tutor' })).toBeEnabled()
    fireEvent.click(screen.getAllByRole('button', { name: /Estou estudando laços de repetição/ })[0])

    expect(screen.getAllByRole('button', { name: /Estou estudando laços de repetição/ }).length).toBeGreaterThan(0)
    expect(await within(screen.getByLabelText('Conversa livre com o tutor')).findByText(/Estou estudando laços/)).toBeInTheDocument()
  })

  it('renders inline code inside a tutor sentence without literal backticks', async () => {
    const session: Session = {
      id: 'inline-code', exerciseId: 'chat-livre', trackId: 'chat-livre', code: '', status: 'open',
      state: { conceptInDoubt: 'range', difficultyType: 'conceito', helpLevel: 1, directSolutionRequests: 0, allowedExposureLevel: 1, shortHistory: [] },
      messages: [
        { id: 'welcome', role: 'tutor', text: 'Olá!', createdAt: '2026-01-01T00:00:00Z' },
        { id: 'student', role: 'student', text: 'Como usar range?', createdAt: '2026-01-01T00:00:01Z' },
        { id: 'answer', role: 'tutor', text: 'A função `range()` pode receber um, dois ou três argumentos.', createdAt: '2026-01-01T00:00:02Z' },
      ],
    }
    vi.spyOn(tutorGateway, 'getSession').mockResolvedValue(session)
    render(<MemoryRouter initialEntries={['/chat?session=inline-code']}><ChatPage /></MemoryRouter>)

    const code = await screen.findByText('range()', { selector: 'code' })
    expect(code.closest('p')).toHaveTextContent('A função range() pode receber um, dois ou três argumentos.')
    expect(code.closest('p')).not.toHaveTextContent('`')
    expect(screen.getAllByText('Passo').length).toBeGreaterThan(0)
    expect(screen.getByText('Aluno')).toBeInTheDocument()
    expect(code.closest('article')!.querySelector('img')).toBeInTheDocument()
  })

  it('keeps suggestions available when reopening a conversation with only the greeting', async () => {
    vi.spyOn(tutorGateway, 'getSession').mockResolvedValue({ id:'greeting',exerciseId:'chat-livre',trackId:'chat-livre',code:'',state:{conceptInDoubt:'',difficultyType:'',helpLevel:1,directSolutionRequests:0,allowedExposureLevel:1,shortHistory:[]},messages:[{id:'welcome',role:'tutor',text:'Olá, vamos conversar?',createdAt:'2026-10-15T12:00:00Z'}] })
    render(<MemoryRouter initialEntries={['/chat?session=greeting']}><ChatPage/></MemoryRouter>)
    expect(await screen.findByText('Olá, vamos conversar?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {name:/Entender um conceito/}))
    expect(screen.getByRole('textbox', {name:'Sua mensagem para o tutor'})).toHaveValue('Estou estudando laços de repetição e ainda não entendi quando usar for ou while.')
  })

  it('clears the composer and offers stop while the tutor is thinking', async () => {
    let accept!: (value: { sessionId: string; jobId: string; messageId: string }) => void
    vi.spyOn(tutorGateway, 'sendChatMessage').mockImplementation(() => new Promise((resolve) => { accept = resolve }))
    render(<MemoryRouter><ChatPage /></MemoryRouter>)
    const input = screen.getByRole('textbox', { name: 'Sua mensagem para o tutor' })
    fireEvent.change(input, { target: { value: 'Minha dúvida' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enviar mensagem' }))
    expect(input).toHaveValue('')
    expect(screen.getByText('Aluno')).toBeInTheDocument()
    expect(screen.getByText('Passo')).toBeInTheDocument()
    expect(input).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Parar resposta' })).toBeEnabled()
    expect(within(screen.getByLabelText('Conversa livre com o tutor')).getByText('Minha dúvida')).toBeInTheDocument()
    accept({ sessionId: 'chat-1', jobId: 'job-1', messageId: 'message-1' })
  })

  it('stops a response, keeps the sent message, and continues it without duplicating the prompt', async () => {
    render(<MemoryRouter><ChatPage /></MemoryRouter>)
    const input = screen.getByRole('textbox', { name: 'Sua mensagem para o tutor' })
    fireEvent.change(input, { target: { value: 'Como funciona um laço?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enviar mensagem' }))
    fireEvent.click(screen.getByRole('button', { name: 'Parar resposta' }))
    expect(await screen.findByRole('button', { name: 'Continuar resposta' })).toBeEnabled()
    expect(input).toBeEnabled()
    expect(within(screen.getByLabelText('Conversa livre com o tutor')).getAllByText('Como funciona um laço?')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Continuar resposta' }))
    expect(input).toBeDisabled()
    await waitFor(() => expect(input).toBeEnabled(), { timeout: 3000 })
    expect(within(screen.getByLabelText('Conversa livre com o tutor')).getAllByText('Como funciona um laço?')).toHaveLength(1)
  })

  it('recovers a running response after reloading and can stop it', async () => {
    const running: Session = {
      id: 'running-chat', exerciseId: 'chat-livre', trackId: 'chat-livre', code: '', status: 'open',
      state: { conceptInDoubt: '', difficultyType: '', helpLevel: 1, directSolutionRequests: 0, allowedExposureLevel: 1, shortHistory: [], countableTurns: 0 },
      messages: [{ id: 'message-1', role: 'student', text: 'Mensagem enviada', createdAt: '2026-01-01T00:00:00Z' }],
      generation: { jobId: 'job-1', messageId: 'message-1', status: 'running' },
    }
    vi.spyOn(tutorGateway, 'getSession').mockResolvedValue(running)
    const stop = vi.spyOn(tutorGateway, 'stopChatResponse').mockResolvedValue({ ...running, generation: { ...running.generation!, status: 'cancelled' } })
    render(<MemoryRouter initialEntries={['/chat?session=running-chat']}><ChatPage /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Parar resposta' })).toBeEnabled()
    expect(screen.getByRole('textbox', { name: 'Sua mensagem para o tutor' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Parar resposta' }))
    expect(await screen.findByRole('button', { name: 'Continuar resposta' })).toBeEnabled()
    expect(stop).toHaveBeenCalledWith('running-chat', 'job-1')
  })
})

describe('ChatPage probes', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })
  const base: Session = {
    id: 'c1', exerciseId: 'chat-livre', status: 'open',
    messages: [
      { id: 's1', role: 'student', text: 'Resolvi a regra', createdAt: '2026-10-18T10:00:00Z', probeIndex: 0 },
      { id: 't1', role: 'tutor', text: 'Você explicou a regra da lacuna.', createdAt: '2026-10-18T10:00:01Z', probeIndex: 0 },
    ],
    state: { conceptInDoubt: 'seleção', difficultyType: '', helpLevel: 1, directSolutionRequests: 0, allowedExposureLevel: 1, shortHistory: [], mode: 'CLOSING_RESOLVED', status: 'encerrada_resolvida', countableTurns: 1, gapResolved: true },
  }

  it('keeps the composer open after a probe closes so a new doubt can start', async () => {
    vi.spyOn(tutorGateway, 'getSession').mockResolvedValue(base)
    vi.spyOn(tutorGateway, 'getSessions').mockResolvedValue([])
    render(<MemoryRouter initialEntries={['/chat?session=c1']}><ChatPage /></MemoryRouter>)
    await screen.findByText('Você explicou a regra da lacuna.')
    expect(screen.getByRole('textbox', { name: 'Sua mensagem para o tutor' })).toBeEnabled()
    expect(screen.queryByText(/Esta conversa foi encerrada/)).not.toBeInTheDocument()
  })
})
