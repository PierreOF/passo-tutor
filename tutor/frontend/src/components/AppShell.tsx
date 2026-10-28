import { History, Menu, MessageCircleQuestion, Moon, Sun, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useOptionalAuth } from '../auth/hooks'

export function AppShell() {
  const [open, setOpen] = useState(false)
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('passo:theme')
    if (saved === 'light' || saved === 'dark') return saved
    return 'dark'
  })
  const location = useLocation()
  const auth = useOptionalAuth()
  const navigate = useNavigate()
  const inWorkspace = location.pathname === '/chat'

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    document.documentElement.style.colorScheme = theme
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#141019' : '#f8f6fc')
    localStorage.setItem('passo:theme', theme)
  }, [theme])

  return (
    <div className={'app-shell' + (inWorkspace ? '' : ' page-layout')}>
      <a className="skip-link" href="#main-content">Pular para o conteúdo</a>
      <header className="site-header">
        <Link className="brand" to="/" aria-label="Passo, página inicial">
          <span className="brand-mark" aria-hidden="true"><span /></span>
          <span>passo<span className="brand-dot">.</span></span>
        </Link>
        <nav className={open ? 'main-nav is-open' : 'main-nav'} aria-label="Navegação principal">
          <NavLink to="/" end onClick={() => setOpen(false)}>Início</NavLink>
          {auth?.user?.role !== 'admin' && <NavLink to="/chat" onClick={() => setOpen(false)}><MessageCircleQuestion size={17} /> Chat com o tutor</NavLink>}
          {auth?.user?.role === 'student' && <NavLink to="/historico" onClick={() => setOpen(false)}><History size={17} /> Histórico</NavLink>}
        </nav>
        <button className="theme-toggle" type="button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} aria-label={`Ativar tema ${theme === 'light' ? 'escuro' : 'claro'}`} title={`Ativar tema ${theme === 'light' ? 'escuro' : 'claro'}`}>
          {theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}
        </button>
        <Link className={'button button-small header-cta' + (!auth?.user ? ' public-login' : '')} to={!auth?.user ? '/login' : auth.user.role === 'admin' ? '/admin' : '/chat'}>{!auth?.user ? 'Fazer login' : auth.user.role === 'admin' ? 'Painel de revisão' : 'Abrir chat'}</Link>
        {auth?.user && <button className="account-button" type="button" onClick={() => { navigate('/'); void auth.logout() }} title="Encerrar sessão">{auth.user.studentCode} · Sair</button>}
        <button className="menu-button" type="button" onClick={() => setOpen(!open)} aria-label={open ? 'Fechar menu' : 'Abrir menu'} aria-expanded={open}>
          {open ? <X /> : <Menu />}
        </button>
      </header>
      <main id="main-content"><Outlet /></main>
      {!inWorkspace && (
        <footer className="site-footer">
          <div className="brand footer-brand"><span className="brand-mark" aria-hidden="true"><span /></span><span>passo<span className="brand-dot">.</span></span></div>
          <p>Aprender programação é construir caminhos, não copiar chegadas.</p>
          {auth?.user?.role === 'student' && <Link className="footer-research-link" to="/historico"><History size={15} /> Meu histórico</Link>}
          <p className="footer-meta">Tutor socrático para Programação I</p>
        </footer>
      )}
    </div>
  )
}
