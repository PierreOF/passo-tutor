# Tutor socrático multiagente (SELA)

Código do tutor socrático e do instrumento de avaliação do Trabalho de Conclusão de Curso *Projeto e avaliação de um tutor socrático multiagente baseado em LLM: controle da entrega direta de soluções em introdução à programação*, de Marcos Pierre de Melo Monteiro (Bacharelado em Engenharia de Software, Universidade de Pernambuco, *Campus* Garanhuns, 2026), sob orientação de Aêda Monalliza Cunha de Sousa.

O tutor conversa com estudantes de introdução à programação (Python) sem entregar a solução pronta. Ele materializa em cinco agentes LLM a Socratic Evaluation Layered Architecture (SELA), proposta por Sousa (2026, relatório de pós-doutorado não publicado): o Nível 0 delimita o escopo curricular e diagnostica a lacuna do estudante, o Nível 2 acompanha o estado da sessão e planeja o próximo passo, e o Nível 1 gera cada pergunta e a valida antes da publicação. Uma barreira determinística decide o que pode ser mostrado em cada modo da sessão.

## Conteúdo

| Caminho | Conteúdo |
| --- | --- |
| [`tutor/src/tutor_socratico/`](tutor/src/tutor_socratico/) | sistema avaliado: contratos, níveis 0, 1 e 2 da SELA, orquestrador, API HTTP, PostgreSQL e executor de código |
| [`tutor/src/instrumento_avaliacao/`](tutor/src/instrumento_avaliacao/) | instrumento: aluno simulado, qualificação Replay, campanha, verificadores, juízes LLM e regras de veredito |
| [`tutor/avaliacao/`](tutor/avaliacao/README.md) | entradas versionadas: exercícios com oráculos, perfis, roteiros Replay, registro de modelos e rubrica dos juízes |
| [`tutor/tests/`](tutor/tests/) | suíte automatizada, sem chamadas a modelos |
| [`tutor/frontend/`](tutor/frontend/README.md) | interface web em React e TypeScript |
| [`tutor/docs/desenvolvimento/`](tutor/docs/desenvolvimento/) | operação local e diagramas em draw.io |
| [`pesquisa/11_avaliacao/`](pesquisa/11_avaliacao/README.md) | pasta onde o instrumento grava os resultados, vazia e fora do Git |

O [README do tutor](tutor/README.md) descreve a arquitetura, o fluxo de um turno e a avaliação com diagramas.

## Como executar

Requisitos: Python 3.12, Docker com Compose e Node.js compatível com o Vite 8 (desenvolvido com o Node.js 24). Os comandos partem de `tutor/`.

```bash
cd tutor
cp .env.example .env                                      # defina a senha do PostgreSQL e a chave do OpenRouter
PYTHONPATH=src python -m unittest discover -s tests -v    # suíte offline
docker compose up -d --build                              # PostgreSQL e backend
cd frontend && npm install && npm run dev                 # interface web
```

A criação do administrador, os alunos de teste e a configuração dos modelos estão em [`operacao_mvp.md`](tutor/docs/desenvolvimento/operacao_mvp.md); a qualificação e a campanha com alunos simulados, em [`tutor/avaliacao/README.md`](tutor/avaliacao/README.md). As execuções reais chamam modelos pagos pelo OpenRouter.

## Relação com o TCC

Este repositório publica, com histórico próprio e sem os documentos internos da pesquisa, a versão usada na campanha de avaliação de 26/10/2026, que o manifesto da campanha registra pelo commit `451b81c` do repositório de desenvolvimento. Em relação a esse commit, `tutor/src/`, `tutor/frontend/` e os arquivos de dados de `tutor/avaliacao/` são idênticos. Mudaram só a documentação e um teste: `test_equivalence_record_matches_registry_at_recorded_commit` consulta o registro de modelos num commit do histórico de desenvolvimento e, aqui, é pulado. Alguns comentários do código citam documentos de método ou decisões internas que não fazem parte desta publicação; o método está descrito no capítulo 3 e nos apêndices do TCC.

Os registros brutos das conversas e dos pareceres da campanha não são publicados. As chaves de acesso aos provedores ficam só no `.env` local.

## Licença

O código é distribuído sob a [licença MIT](LICENSE). Os logotipos usados nos diagramas pertencem aos seus titulares, com as licenças indicadas no [README dos diagramas](tutor/docs/desenvolvimento/diagramas/README.md).
