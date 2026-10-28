export interface ReviewRun { run_id: string; kind: string; version: string | null; case_count: number; warnings: string[]; started_at?: string }
interface ReviewMessage { role: 'student' | 'tutor'; text: string; turn: number; created_at?: string; code?: string; code_unavailable?: boolean; pending?: boolean; response_missing?: boolean }
export interface ReviewCase { run_id: string; case_id: string; exercise_id: string | null; profile_id: string | null; scenario: string | null; repetition: number | null; status: string; technical_result: string | null; turn_count: number; source_ref: string; cost_usd: number | null; closing: Record<string, unknown>; error: Record<string, unknown>; reconciliation?: Record<string, unknown>; gap_summary?: { gap_type?: string; claim_text?: string } | null }
export interface ReviewDetail extends ReviewCase { messages: ReviewMessage[]; details: Record<string, unknown>; runtime?: Record<string, unknown>; warnings: string[] }
interface Page<T> { items: T[]; total: number; offset: number; limit: number; warnings?: string[] }
async function get<T>(path: string): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 15000)
  try {
    const response = await fetch(path, { credentials: 'same-origin', signal: controller.signal })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({})) as { message?: string }
      throw new Error(payload.message ?? (response.status === 403 ? 'Acesso reservado ao administrador.' : 'Não foi possível consultar os registros. Confira o acesso e os artefatos deste ambiente.'))
    }
    return await response.json() as T
  } catch (error) {
    if (controller.signal.aborted) throw new Error('A consulta demorou demais. Tente atualizar a lista.')
    throw error
  } finally {
    window.clearTimeout(timer)
  }
}
async function all<T>(path: string): Promise<{ items: T[]; warnings: string[] }> {
  const items: T[] = []; const warnings = new Set<string>()
  let offset = 0
  while (true) {
    const page = await get<Page<T>>(path + '?limit=200&offset=' + offset)
    items.push(...page.items); page.warnings?.forEach(w => warnings.add(w))
    offset += page.items.length
    if (offset >= page.total || !page.items.length) break
  }
  return { items, warnings: [...warnings] }
}
export const experimentReviewApi = {
  runs: async () => (await all<ReviewRun>('/api/admin/experiments')).items,
  cases: (run: string) => all<ReviewCase>('/api/admin/experiments/' + encodeURIComponent(run) + '/cases'),
  case: (run: string, id: string) => get<ReviewDetail>('/api/admin/experiments/' + encodeURIComponent(run) + '/cases/' + encodeURIComponent(id)),
}
