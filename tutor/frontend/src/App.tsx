import { Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import { RequireAuth } from './auth/RequireAuth'
import { AppShell } from './components/AppShell'
import { AdminPage } from './pages/AdminPage'
import { AdminRecordsPage } from './pages/AdminRecordsPage'
import { ChatPage } from './pages/ChatPage'
import { ChangePasswordPage } from './pages/ChangePasswordPage'
import { ExperimentReviewPage } from './pages/ExperimentReviewPage'
import { HistoryPage } from './pages/HistoryPage'
import { HomePage } from './pages/HomePage'
import { LoginPage } from './pages/LoginPage'

export default function App() {
  return <AuthProvider><Routes>
    <Route element={<AppShell />}><Route path="/" element={<HomePage />} /></Route>
    <Route path="/login" element={<LoginPage />} />
    <Route element={<RequireAuth />}>
      <Route path="/alterar-senha" element={<ChangePasswordPage />} />
      <Route element={<RequireAuth role="student" />}>
        <Route element={<AppShell />}>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/historico" element={<HistoryPage />} />
        </Route>
      </Route>
      <Route element={<RequireAuth role="admin" />}>
        <Route element={<AppShell />}>
        <Route path="/admin" element={<AdminPage />} />
        <Route path="/admin/experimentos" element={<ExperimentReviewPage />} />
        <Route path="/admin/conversas" element={<AdminRecordsPage />} />
        <Route path="/pesquisa" element={<Navigate to="/admin/experimentos" replace />} />
        </Route>
      </Route>
    </Route>
    {/* Endereço desconhecido leva à página inicial pública, não ao login. */}
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></AuthProvider>
}
