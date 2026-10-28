import { useLayoutEffect } from 'react'
import type { ReviewDetail } from '../services/experimentReviewApi'
import { ConversationMessages } from './ConversationMessages'
import { exerciseLabel, groupLabel, groupOf, statusLabel } from '../domain/reviewLabels'
export function ExperimentTranscript({ detail }: { detail: ReviewDetail }) {
  useLayoutEffect(() => { window.scrollTo({ top: 0, behavior: 'instant' }) }, [detail.run_id, detail.case_id])
  return <section className="review-transcript" aria-label="Histórico do experimento">
    <header className="review-conversation-heading"><div><p className="eyebrow">Histórico preservado</p><h2>{detail.repetition == null ? detail.case_id : 'Conversa ' + detail.repetition}</h2><small>{detail.case_id}</small></div><span className="review-badge">Somente leitura</span></header>
    <dl className="review-case-context" aria-label="Contexto da conversa">
      <div><dt>Tipo de aluno</dt><dd>{groupLabel(groupOf(detail))}</dd></div>
      <div><dt>Exercício</dt><dd>{detail.exercise_id ? detail.exercise_id + ' — ' + exerciseLabel(detail.exercise_id) : exerciseLabel('')}</dd></div>
      <div><dt>Situação</dt><dd>{statusLabel(detail.status)}</dd></div>
      <div><dt>Lacuna</dt><dd>{detail.gap_summary?.gap_type ? <><code>{detail.gap_summary.gap_type}</code>{detail.gap_summary.claim_text ? ' — ' + detail.gap_summary.claim_text : ''}</> : 'Não registrada'}</dd></div>
    </dl>
    {!!Object.keys(detail.error).length && <p className="review-notice" role="status">Interrupção: {String(detail.error.kind ?? detail.error.stage ?? 'registrada')}</p>}
    {!detail.messages.length && <p>Nenhuma mensagem preservada neste registro.</p>}
    <ConversationMessages review profileId={detail.profile_id} studentName={detail.profile_id ? undefined : detail.scenario ? 'Aluno · ' + detail.scenario : 'Aluno simulado'} messages={detail.messages.map((message, index) => ({
      id: detail.case_id + '-' + index, role: message.role, text: message.text, turn: message.turn,
      createdAt: message.created_at, code: message.code, codeUnavailable: message.code_unavailable,
      pending: message.pending, responseMissing: message.response_missing,
    }))}/>
    <details className="review-technical"><summary>Detalhes técnicos</summary><p>{detail.turn_count} turnos · Execução: {statusLabel(detail.status)}</p><p>Resultado técnico: {detail.technical_result ?? 'Não registrado'}</p><small>{detail.source_ref}</small><p>Custo registrado: {detail.cost_usd == null ? 'Não disponível' : 'US$ ' + detail.cost_usd}</p><p>O encerramento informado pelo tutor se refere à lacuna e não certifica o exercício inteiro.</p><pre>{JSON.stringify({ encerramento: detail.closing, reconciliacao: detail.reconciliation, ...detail.details, runtime: detail.runtime }, null, 2)}</pre></details>
  </section>
}
