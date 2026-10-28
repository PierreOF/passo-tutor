// Rótulos da revisão de experimentos: identificadores dos instrumentos em `avaliacao/` traduzidos para leitura humana.
const profiles: Record<string, string> = {
  P01: 'Busca de solução pronta', P02: 'Concepção equivocada', P03: 'Compreensão apenas aparente',
  P04: 'Depuração de código', P05: 'Dificuldade de iniciar',
}
// Cenários da qualificação Replay (`avaliacao/roteiros/matriz_principal.csv`): código S0n e descrição.
const scenarios: Record<string, [string, string]> = {
  pedido_direto: ['S01', 'Pedido direto da solução'], persistencia: ['S02', 'Persistência'],
  prompt_injection: ['S03', 'Injeção de prompt'], erro_codigo: ['S04', 'Código com erro'], travamento: ['S05', 'Aluno travado'],
}
const scenarioByCode = Object.fromEntries(Object.values(scenarios))
const exercises: Record<string, string> = { E01: 'Par ou ímpar', E02: 'Média dos positivos', E03: 'Maior valor' }
const statuses: Record<string, string> = {
  COMPLETO: 'Completo', INCONCLUSIVO: 'Inconclusivo', SEM_CONCLUSAO: 'Sem conclusão',
  ENTRADA_FORA_DO_ESCOPO: 'Entrada fora do escopo', FORA_DO_CATALOGO: 'Fora do catálogo (histórico)',
}

const NO_GROUP = 'sem-perfil'
export const NO_EXERCISE = 'sem-exercicio'

function profileLabel(id: string): string | undefined { return profiles[id] }
function scenarioLabel(scenario: string): string | undefined { return scenarios[scenario]?.[1] ?? scenarioByCode[scenario] }
export function exerciseLabel(id: string): string { return exercises[id] ?? 'Exercício não informado' }
export function statusLabel(status: string): string { return statuses[status] ?? status }

/** Pasta do tipo de aluno: perfil simulado (P0n) ou, no Replay, o código do cenário (S0n). */
export function groupOf(item: { profile_id: string | null; scenario: string | null }): string {
  if (item.profile_id) return item.profile_id
  if (item.scenario) return scenarios[item.scenario]?.[0] ?? item.scenario
  return NO_GROUP
}
export function groupLabel(group: string): string {
  const label = profileLabel(group) ?? scenarioLabel(group)
  return label ? group + ' — ' + label : group
}
