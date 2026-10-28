import { FolderOpen, MessagesSquare } from 'lucide-react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'

export function AdminPage() {
  const [params] = useSearchParams()
  if (params.has('run')) return <Navigate to={'/admin/experimentos?' + params.toString()} replace />
  return <section className="admin-page admin-home">
    <h1>Painel do administrador</h1>
    <Link className="admin-experiments-entry" to="/admin/experimentos"><FolderOpen size={28}/><span>Experimentos</span></Link>
    {/* Seção secundária: o painel é voltado à revisão dos pilotos e testes; as contas são criadas pelo CLI. */}
    <Link className="admin-secondary-entry" to="/admin/conversas"><MessagesSquare size={18}/><span>Conversas do chat</span></Link>
  </section>
}
