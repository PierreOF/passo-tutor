import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { HomePage } from './HomePage'
afterEach(cleanup)
it('presents the academic prototype with chat access and no learning tracks', () => {
  render(<MemoryRouter><HomePage /></MemoryRouter>)
  expect(screen.getByRole('link', { name: /Conversar com o tutor/ })).toHaveAttribute('href', '/chat')
  expect(screen.queryByText('Escolha uma trilha')).not.toBeInTheDocument()
  expect(screen.queryAllByRole('link').some(a => a.getAttribute('href')?.startsWith('/exercicios'))).toBe(false)
})
it('shows an example where the tutor only asks questions and no demo logins', () => {
  render(<MemoryRouter><HomePage /></MemoryRouter>)
  expect(screen.queryByText('Acesso de demonstração')).not.toBeInTheDocument()
  expect(screen.queryByText('admin123')).not.toBeInTheDocument()
  expect(screen.queryByText(/Protótipo acadêmico/)).not.toBeInTheDocument()
  const tutorTurns = document.querySelectorAll('.hero-demo .conversation-message.tutor p')
  expect(tutorTurns.length).toBeGreaterThan(0)
  tutorTurns.forEach((turn) => expect(turn.textContent?.trim().endsWith('?')).toBe(true))
})
