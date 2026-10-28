import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { authApi, type AuthState } from './services/authApi'
vi.mock('./services/authApi', () => ({ authApi: { me: vi.fn(), login: vi.fn(), logout: vi.fn(async () => {}), changePassword: vi.fn(async () => {}) } }))
vi.mock('./services/experimentReviewApi', () => ({ experimentReviewApi: { runs: vi.fn(async () => []), cases: vi.fn(async () => ({items: [], warnings: []})) } }))
vi.mock('./services/gateway', () => ({ tutorGateway: { getSessions: vi.fn(async () => []), getSession: vi.fn(async () => ({ id: 'old', code: '', messages: [{id: 'm', role: 'student', text: 'Minha conversa antiga'}] })) } }))
const student: AuthState = { user: {id:'s', studentCode:'aluno', role:'student', active:true}, mustChangePassword:false }
const admin: AuthState = { user: {id:'a', studentCode:'admin', role:'admin', active:true}, mustChangePassword:false }
beforeEach(() => { vi.mocked(authApi.me).mockResolvedValue({user:null, mustChangePassword:false}) })
afterEach(cleanup)
function open(path: string) { return render(<MemoryRouter initialEntries={[path]}><App/></MemoryRouter>) }
it('opens the public homepage without redirecting to login', async () => {
  open('/')
  expect(await screen.findByRole('link', {name:'Fazer login'})).toBeInTheDocument()
  expect(screen.getByRole('heading', {name:/Uma pergunta/})).toBeInTheDocument()
})
it('requires authentication for chat and admin', async () => {
  open('/admin?run=private')
  expect(await screen.findByRole('heading', {name:'Entre na sua conta'})).toBeInTheDocument()
  expect(screen.queryByText('Revisão de experimentos')).not.toBeInTheDocument()
})
it('sends a student to chat after login', async () => {
  vi.mocked(authApi.login).mockResolvedValue(student)
  open('/login')
  fireEvent.change(screen.getByLabelText('Código de aluno'), {target:{value:'aluno'}})
  fireEvent.change(screen.getByLabelText('Senha'), {target:{value:'abcdef'}})
  fireEvent.click(screen.getByRole('button', {name:'Entrar'}))
  expect(await screen.findByText('Olá! O que você está estudando hoje?')).toBeInTheDocument()
})
it('opens a single experiments entry for the admin after login', async () => {
  vi.mocked(authApi.me).mockResolvedValue(admin)
  open('/login')
  expect(await screen.findByRole('link', {name:'Experimentos'})).toHaveAttribute('href', '/admin/experimentos')
  expect(screen.queryByRole('navigation', {name:'Seções administrativas'})).not.toBeInTheDocument()
  expect(screen.queryByText('Contas')).not.toBeInTheDocument()
  // Revisão da pesquisa em primeiro plano; conversas reais do chat ficam como seção secundária.
  expect(screen.getByRole('link', {name:'Conversas do chat'})).toHaveAttribute('href', '/admin/conversas')
  expect(screen.getByRole('banner')).toBeInTheDocument()
  expect(screen.getAllByRole('button', {name:/Sair/})).toHaveLength(1)
  expect(screen.getByRole('button', {name:/Sair/}).closest('header')).not.toBeNull()
})
it('sends old exercise links to the homepage: the tool has only the tutor chat', async () => {
  vi.mocked(authApi.me).mockResolvedValue(student)
  open('/exercicios/condicionais')
  expect(await screen.findByRole('heading', {name:/Uma pergunta/})).toBeInTheDocument()
})
it('requires temporary password change before admin review', async () => {
  vi.mocked(authApi.me).mockResolvedValue({...admin, mustChangePassword:true})
  open('/admin')
  expect(await screen.findByRole('heading', {name:'Crie sua senha'})).toBeInTheDocument()
})
it('sends visitors on unknown routes to the homepage instead of the login', async () => {
  open('/rota-que-nao-existe')
  expect(await screen.findByRole('heading', {name:/Uma pergunta/})).toBeInTheDocument()
  expect(screen.queryByRole('heading', {name:'Entre na sua conta'})).not.toBeInTheDocument()
})
