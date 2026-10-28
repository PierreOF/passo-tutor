import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { AppShell } from './AppShell'

describe('AppShell theme', () => {
  beforeEach(() => localStorage.setItem('passo:theme', 'light'))
  afterEach(() => {
    cleanup()
    document.documentElement.removeAttribute('data-theme')
  })

  it('persists the selected dark theme', () => {
    render(<MemoryRouter><AppShell /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Ativar tema escuro' }))
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('passo:theme')).toBe('dark')
  })

  it('starts in dark theme when there is no saved preference', () => {
    localStorage.removeItem('passo:theme')
    render(<MemoryRouter><AppShell /></MemoryRouter>)

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(screen.getByRole('button', { name: 'Ativar tema claro' })).toBeInTheDocument()
  })

  it('offers login to visitors without learning tracks', () => {
    render(<MemoryRouter><AppShell /></MemoryRouter>)

    expect(within(screen.getByRole('navigation', { name: 'Navegação principal' })).queryByText('Área de pesquisa')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Fazer login' })).toHaveAttribute('href', '/login')
    expect(within(screen.getByRole('navigation', { name: 'Navegação principal' })).queryByText('Exercícios')).not.toBeInTheDocument()
  })
})
