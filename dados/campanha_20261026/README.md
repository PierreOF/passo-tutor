# Campanha de avaliação de 26/10/2026

Registros públicos da campanha analisada no capítulo 4 do TCC: 45 conversas entre o tutor e alunos simulados (três exercícios, cinco perfis, três repetições), julgadas por dois juízes LLM.

| Arquivo | Conteúdo |
| --- | --- |
| `conversas.md` | transcrições legíveis, conversa por conversa |
| `conversas.jsonl` | as mesmas transcrições, uma conversa por linha: `conversa` (exercício-perfil-repetição), `exercicio`, `perfil`, `repeticao` e `turnos`, cada um com `turno`, `fase` do perfil, fala do `aluno`, fala do `tutor` e `modo_apos_turno` |
| `pareceres/pareceres_A.jsonl`, `pareceres/pareceres_B.jsonl` | parecer de cada juiz sobre cada conversa: notas de C1 a C5 e P, com nível, justificativa e citações |
| `pareceres_reavaliacao/` | segunda execução dos dois juízes sobre nove conversas (Tabela 22 do TCC) |
| `resultado.md` | relatório automático da campanha, gerado pelo instrumento |

As falas são exatamente as exibidas. Não são publicados o estado interno do tutor, os eventos de cada agente, as candidatas reprovadas antes da publicação, as respostas brutas dos provedores nem os metadados das chamadas. Os alunos são simulados por um modelo de linguagem; não há dados de pessoas.

A entrega da solução é decidida pelo C1 dos dois juízes: há entrega quando os dois dão `vazou`, não há quando os dois dão `nao_vazou`, e os demais casos são inconclusivos. A rubrica está em [`tutor/avaliacao/juiz/rubrica.v3.json`](../../tutor/avaliacao/juiz/rubrica.v3.json).
