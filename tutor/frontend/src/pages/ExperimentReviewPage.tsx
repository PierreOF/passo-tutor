import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Folder, MessageSquare, ArrowLeft, RefreshCw } from 'lucide-react'
import { ExperimentTranscript } from '../components/ExperimentTranscript'
import { experimentReviewApi, type ReviewRun, type ReviewCase, type ReviewDetail } from '../services/experimentReviewApi'
import { NO_EXERCISE, exerciseLabel, groupLabel, groupOf, statusLabel } from '../domain/reviewLabels'
const kindLabels: Record<string, string> = { pilot: 'Pilotos', campaign: 'Campanhas', replay: 'Qualificações Replay', historical: 'Históricos sem classificação', unsupported: 'Formato não suportado' }
function runTimestamp(run: ReviewRun): number {
  const timestamp = Date.parse(run.started_at ?? '')
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY
}
function runDateLabel(run: ReviewRun): string {
  return Number.isFinite(runTimestamp(run))
    ? 'Início: ' + new Date(run.started_at!).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' })
    : 'Data não registrada'
}
export function ExperimentReviewPage() {
  const [params, setParams] = useSearchParams()
  const run = params.get('run') ?? ''; const group = params.get('group') ?? ''; const exercise = params.get('exercise') ?? ''; const selected = params.get('case') ?? ''
  const [runs, setRuns] = useState<ReviewRun[]>([]); const [cases, setCases] = useState<ReviewCase[]>([]); const [detail, setDetail] = useState<ReviewDetail | null>(null)
  const [warnings, setWarnings] = useState<string[]>([]); const [error, setError] = useState(''); const [loading, setLoading] = useState(false); const [loadingRuns, setLoadingRuns] = useState(true)
  const [search, setSearch] = useState(''); const [status, setStatus] = useState(''); const [refresh, setRefresh] = useState(0)
  useEffect(() => { let active = true; setLoadingRuns(true); setError(''); void experimentReviewApi.runs().then(value => { if (active) setRuns([...value].sort((a, b) => (runTimestamp(b) - runTimestamp(a)) || b.run_id.localeCompare(a.run_id))) }).catch(e => { if (active) setError(e.message) }).finally(() => { if (active) setLoadingRuns(false) }); return () => { active = false } }, [refresh])
  useEffect(() => {
    let active = true; setCases([]); setWarnings([]); setDetail(null); setError(''); setSearch(''); setStatus('')
    if (!run) { setLoading(false); return }
    setLoading(true)
    void experimentReviewApi.cases(run).then(value => { if (active) { setCases(value.items); setWarnings(value.warnings) } }).catch(e => { if (active) setError(e.message) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [run, refresh])
  useEffect(() => {
    let active = true; setDetail(null)
    if (run && selected) void experimentReviewApi.case(run, selected).then(value => { if (active) setDetail(value) }).catch(e => { if (active) setError(e.message) })
    return () => { active = false }
  }, [run, selected, refresh])
  // Pastas: execução → tipo de aluno (perfil ou cenário) → exercício → conversas.
  function choose(next: {run?: string; group?: string; exercise?: string; case?: string}) {
    const query = new URLSearchParams(params); ['run', 'group', 'exercise', 'case'].forEach(key => query.delete(key))
    Object.entries(next).forEach(([key, value]) => { if (value) query.set(key, value) }); setParams(query); setError('')
  }
  function back() { choose(selected ? {run, group, exercise} : exercise ? {run, group} : group ? {run} : {}) }
  const runInfo = runs.find(r => r.run_id === run)
  const exerciseOf = (c: ReviewCase) => c.exercise_id ?? NO_EXERCISE
  const inGroup = cases.filter(c => groupOf(c) === group)
  const visible = inGroup.filter(c => exerciseOf(c) === exercise && (!status || c.status === status) && c.case_id.toLowerCase().includes(search.toLowerCase()))
  const groups = [...new Set(cases.map(groupOf))].sort()
  const exercises = [...new Set(inGroup.map(exerciseOf))].sort()
  const title = selected ? 'Histórico do chat' : exercise ? 'Chats do aluno' : group ? 'Exercícios' : run ? (runInfo?.kind === 'replay' ? 'Cenários' : 'Tipos de aluno') : 'Experimentos'
  const context = [run, group ? groupLabel(group) : '', exercise ? exercise + ' — ' + exerciseLabel(exercise) : ''].filter(Boolean).join(' / ')
  return <section className="admin-page sequential-review">
    {run ? <button className="review-back" onClick={back}><ArrowLeft size={16}/> Voltar</button> : <Link className="review-back" to="/admin"><ArrowLeft size={16}/> Voltar ao painel</Link>}
    <h1>{title}</h1>
    {context && <p className="review-context">{context}</p>}
    {error && <p className="auth-error" role="alert">{error}</p>}
    {run && warnings.map(w => <p className="review-notice" key={w}>{w}</p>)}
    {!run ? <>
      <button className="review-refresh" onClick={() => setRefresh(v => v + 1)}><RefreshCw size={15}/> Atualizar lista</button>
      {loadingRuns && <p role="status">Carregando experimentos…</p>}
      {!loadingRuns && !runs.length && !error && <p>Nenhuma execução disponível neste ambiente.</p>}
      {Object.entries(kindLabels).map(([kind, label]) => runs.some(r => r.kind === kind) && <section className="experiment-run-group" key={kind}><h2>{label}</h2><div className="review-step-list">{runs.filter(r => r.kind === kind).map(r => <button key={r.run_id} className="review-step-item" onClick={() => choose({run:r.run_id})}><Folder size={22}/><span><strong>{r.run_id}{r.version ? ' · v' + r.version : ''}</strong><small>{Number.isFinite(runTimestamp(r)) ? <time dateTime={r.started_at}>{runDateLabel(r)}</time> : runDateLabel(r)}</small><small>{r.case_count} registros{r.warnings.length ? ' · com avisos' : ''}</small></span></button>)}</div></section>)}
    </> : selected ? <>
      {detail ? <ExperimentTranscript detail={detail}/> : !error && <p role="status">Carregando histórico…</p>}
    </> : loading ? <p role="status">Carregando registros…</p> : !group ? <div className="review-step-list">
      {groups.map(g => <button key={g} className="review-step-item" onClick={() => choose({run, group:g})}><Folder size={24}/><span><strong>{groupLabel(g)}</strong><small>{cases.filter(c => groupOf(c) === g).length} chats registrados</small></span></button>)}
      {!groups.length && !error && <p>Nenhum tipo de aluno registrado neste experimento.</p>}
    </div> : !exercise ? <div className="review-step-list">
      {exercises.map(id => <button key={id} className="review-step-item" onClick={() => choose({run, group, exercise:id})}><Folder size={24}/><span><strong>{id} — {exerciseLabel(id)}</strong><small>{inGroup.filter(c => exerciseOf(c) === id).length} chats registrados</small></span></button>)}
      {!exercises.length && !error && <p>Nenhum exercício registrado para este tipo de aluno.</p>}
    </div> : <>
      <div className="review-chat-filters"><label>Buscar identificador<input value={search} onChange={e => setSearch(e.target.value)} placeholder="Ex.: E01-P01"/></label><label>Situação<select value={status} onChange={e => setStatus(e.target.value)}><option value="">Todas</option>{[...new Set(cases.map(c => c.status))].map(s => <option key={s} value={s}>{statusLabel(s)}</option>)}</select></label></div>
      <div className="review-step-list">{visible.map(c => <button key={c.case_id} className="review-step-item" onClick={() => choose({run, group, exercise, case:c.case_id})}><MessageSquare size={24}/><span><strong>{c.repetition == null ? c.case_id : 'Conversa ' + c.repetition}</strong><small>{c.case_id}</small><small>{statusLabel(c.status)} · {c.turn_count} turnos</small></span></button>)}</div>
      {!visible.length && <p>Nenhum chat para esta seleção.</p>}
    </>}
  </section>
}
