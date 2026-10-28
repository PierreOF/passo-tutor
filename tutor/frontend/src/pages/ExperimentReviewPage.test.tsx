import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { experimentReviewApi } from '../services/experimentReviewApi'
import { ExperimentReviewPage } from './ExperimentReviewPage'

vi.mock('../services/experimentReviewApi', () => ({ experimentReviewApi: {
  runs: vi.fn(async () => [{ run_id: 'piloto-v2', kind: 'pilot', version: '2', case_count: 1, warnings: [] }]),
  cases: vi.fn(async () => ({ items: [{run_id: 'piloto-v2', case_id: 'E01-P01-R01', exercise_id: 'E01', profile_id: 'P01', scenario: null, repetition: 1, status: 'INCONCLUSIVO', technical_result: null, turn_count: 1, source_ref: 'raw:1', cost_usd: null, closing: {}, error: {} }], warnings: [] })),
  case: vi.fn(async () => ({ run_id: 'piloto-v2', case_id: 'E01-P01-R01', exercise_id: 'E01', profile_id: 'P01', status: 'INCONCLUSIVO', messages: [{role: 'student', text: 'Preciso de ajuda', turn: 1, code: 'print(1)', response_missing: true}], details: {}, error: {}, closing: {}, source_ref: 'raw:1', warnings: [] })),
} }))
afterEach(cleanup)
describe('Revisão experimental', () => {
  it('opens execution, student type, exercise and an independent chat', async () => {
    render(<MemoryRouter><ExperimentReviewPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', {name: /piloto-v2/}))
    expect(screen.getByRole('heading', {name:'Tipos de aluno'})).toBeInTheDocument()
    expect(screen.queryByRole('button', {name:/Conversa 1/})).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', {name: /P01 — Busca de solução pronta/}))
    expect(screen.getByRole('heading', {name:'Exercícios'})).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', {name: /E01 — Par ou ímpar/}))
    expect(screen.getByRole('heading', {name:'Chats do aluno'})).toBeInTheDocument()
    expect(screen.queryByRole('button', {name:/P01 — Busca de solução pronta/})).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', {name: /Conversa 1/}))
    expect(await screen.findByText('Preciso de ajuda')).toBeInTheDocument()
    expect(screen.getByText('Resposta não registrada')).toBeInTheDocument()
    expect(screen.getByText('print(1)')).toBeInTheDocument()
    expect(screen.getByText('Aluno simulado · P01')).toBeInTheDocument()
    expect(screen.getByText('Somente leitura')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', {name:/Enviar|Parar|Continuar/})).toBeNull()
    expect(screen.getByText('Detalhes técnicos').closest('details')).not.toHaveAttribute('open')
    fireEvent.click(screen.getByRole('button', {name:'Voltar'}))
    expect(await screen.findByRole('button', {name:/Conversa 1/})).toBeInTheDocument()
  })
  it('keeps chats without exercise metadata visible in their folder', async () => {
    vi.mocked(experimentReviewApi.cases).mockResolvedValueOnce({ items: [{run_id:'piloto-v2', case_id:'old-case', exercise_id:null, profile_id:'P01', scenario:null, repetition:1, status:'INCONCLUSIVO', technical_result:null, turn_count:0, source_ref:'raw:1', cost_usd:null, closing:{}, error:{}}], warnings: [] })
    render(<MemoryRouter><ExperimentReviewPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', {name:/piloto-v2/}))
    fireEvent.click(await screen.findByRole('button', {name:/P01 — Busca de solução pronta/}))
    fireEvent.click(await screen.findByRole('button', {name:/sem-exercicio — Exercício não informado/}))
    expect(await screen.findByRole('button', {name:/old-case/})).toBeInTheDocument()
  })
  it('shows five student types, three exercises, then three independent chats in a complete campaign', async () => {
    const items = [1,2,3].flatMap(e => [1,2,3,4,5].flatMap(p => [1,2,3].map(r => ({ run_id:'campanha', case_id:'E0' + e + '-P0' + p + '-R0' + r, exercise_id:'E0' + e, profile_id:'P0' + p, scenario:null, repetition:r, status:'COMPLETO', technical_result:null, turn_count:4, source_ref:'raw:1', cost_usd:null, closing:{}, error:{} }))))
    vi.mocked(experimentReviewApi.cases).mockResolvedValueOnce({items, warnings:[]})
    render(<MemoryRouter initialEntries={['/admin/experimentos?run=campanha']}><ExperimentReviewPage /></MemoryRouter>)
    expect(await screen.findAllByRole('button', {name:/P0[1-5] —/})).toHaveLength(5)
    fireEvent.click(screen.getByRole('button', {name:/P01 — Busca de solução pronta/}))
    expect(await screen.findAllByRole('button', {name:/E0[123] —/})).toHaveLength(3)
    fireEvent.click(screen.getByRole('button', {name:/E01 — Par ou ímpar/}))
    expect(await screen.findAllByRole('button', {name:/Conversa [123]/})).toHaveLength(3)
    expect(screen.queryByRole('button', {name:/P0[1-5] —/})).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {name:'Voltar'}))
    expect(await screen.findAllByRole('button', {name:/E0[123] —/})).toHaveLength(3)
  })
  it('stops displaying loading when the experiment list fails', async () => {
    vi.mocked(experimentReviewApi.runs).mockRejectedValueOnce(new Error('Falha ao consultar registros'))
    render(<MemoryRouter><ExperimentReviewPage /></MemoryRouter>)
    expect(await screen.findByRole('alert')).toHaveTextContent('Falha ao consultar registros')
    expect(screen.queryByText('Carregando experimentos…')).not.toBeInTheDocument()
  })
  it('positions a newly loaded episode at the beginning even when the previous page was scrolled', async () => {
    let scrollY = 420
    const scroll = vi.spyOn(window, 'scrollTo').mockImplementation((options: ScrollToOptions | number) => {
      if (typeof options === 'object') scrollY = options.top ?? scrollY
    })
    render(<MemoryRouter initialEntries={['/admin/experimentos?run=piloto-v2&exercise=E01&group=P01&case=E01-P01-R01']}><ExperimentReviewPage /></MemoryRouter>)
    await screen.findByText('Preciso de ajuda')
    expect(scrollY).toBe(0)
    scroll.mockRestore()
  })
  it('restores selected chat from the URL', async () => {
    render(<MemoryRouter initialEntries={['/admin?run=piloto-v2&exercise=E01&group=P01&case=E01-P01-R01']}><ExperimentReviewPage /></MemoryRouter>)
    expect(await screen.findByText('Preciso de ajuda')).toBeInTheDocument()
  })
  it('labels replay scenarios and shows profile, exercise, situation and gap in the conversation header', async () => {
    vi.mocked(experimentReviewApi.cases).mockResolvedValueOnce({ items: [{run_id:'qualificacao', case_id:'E02-S05', exercise_id:'E02', profile_id:null, scenario:'travamento', repetition:null, status:'ENTRADA_FORA_DO_ESCOPO', technical_result:'PASS', turn_count:10, source_ref:'raw:1', cost_usd:null, closing:{}, error:{}}], warnings: [] })
    vi.mocked(experimentReviewApi.case).mockResolvedValueOnce({ run_id:'qualificacao', case_id:'E02-S05', exercise_id:'E02', profile_id:null, scenario:'travamento', repetition:null, status:'ENTRADA_FORA_DO_ESCOPO', technical_result:'PASS', turn_count:10, source_ref:'raw:1', cost_usd:null, closing:{}, error:{}, messages:[{role:'student', text:'Estou travado', turn:1}], details:{}, warnings:[], gap_summary:{gap_type:'sem_evidencia', claim_text:'não sabe por onde começar'} })
    render(<MemoryRouter initialEntries={['/admin/experimentos?run=qualificacao']}><ExperimentReviewPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', {name:/S05 — Aluno travado/}))
    fireEvent.click(await screen.findByRole('button', {name:/E02 — Média dos positivos/}))
    fireEvent.click(await screen.findByRole('button', {name:/E02-S05/}))
    const header = await screen.findByLabelText('Contexto da conversa')
    expect(header).toHaveTextContent('Aluno travado')
    expect(header).toHaveTextContent('Média dos positivos')
    expect(header).toHaveTextContent('Entrada fora do escopo')
    expect(header).toHaveTextContent('sem_evidencia')
    expect(header).toHaveTextContent('não sabe por onde começar')
  })
})
