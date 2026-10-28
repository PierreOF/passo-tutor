import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { ConversationMessages } from './ConversationMessages'
afterEach(cleanup)
it('preserves order, author, opening, code, time and audit warnings in read-only mode', () => {
 const { container } = render(<ConversationMessages review profileId="P01" messages={[
  {id:'0',role:'tutor',text:'Olá!',turn:0,createdAt:'2026-10-15T12:00:00Z'},
  {id:'1',role:'student',text:'<script>teste</script>',code:'print(1)',turn:1,pending:true,responseMissing:true,codeUnavailable:true},
  {id:'2',role:'tutor',text:'Observe a função range().',kind:'hint'},
 ]} />)
 expect(screen.getAllByRole('article').map(a=>a.textContent)).toEqual([expect.stringContaining('Olá!'),expect.stringContaining('<script>teste</script>'),expect.stringContaining('Observe a função range().')])
 expect(screen.getAllByText('Passo')).toHaveLength(2)
 expect(screen.getByText('Aluno simulado · P01')).toBeInTheDocument()
 expect(container.querySelector('time')).toHaveAttribute('datetime','2026-10-15T12:00:00Z')
 expect(container.querySelector('script')).toBeNull()
 expect(screen.getByText('print(1)')).toBeInTheDocument()
 expect(screen.getByText('Resposta não registrada')).toBeInTheDocument()
 expect(screen.getByText('Conteúdo do código não preservado neste artefato.')).toBeInTheDocument()
 expect(screen.getByText('Envio pendente de confirmação')).toBeInTheDocument()
 expect(screen.getByText('abertura')).toBeInTheDocument()
 expect(screen.queryByRole('textbox')).toBeNull()
 expect(screen.queryByRole('button')).toBeNull()
})
it('uses the generic student identity without inventing a simulated profile', () => {
 const { container } = render(<ConversationMessages messages={[{id:'1',role:'student',text:'Oi'}]} />)
 expect(screen.getByText('Aluno')).toBeInTheDocument()
 expect(container.querySelector('img')!.src).toContain('student-default.svg')
})
it('marks a new probe and an out-of-scope reply in the same conversation', () => {
 render(<ConversationMessages messages={[
  {id:'1',role:'student',text:'Dúvida 1',probeIndex:0},
  {id:'2',role:'tutor',text:'Síntese.',probeIndex:0},
  {id:'3',role:'student',text:'Dúvida 2',probeIndex:1},
  {id:'4',role:'tutor',text:'Posso ajudar com dúvidas de introdução à programação.',kind:'out_of_scope',probeIndex:1},
 ]} />)
 expect(screen.getAllByRole('separator')).toHaveLength(1)
 expect(screen.getByRole('separator')).toHaveTextContent('Nova dúvida')
 expect(screen.getByText('Fora do escopo da disciplina')).toBeInTheDocument()
})
