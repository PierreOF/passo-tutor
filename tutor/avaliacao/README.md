# Pacote de verificação técnica com alunos simulados

Este diretório operacionaliza o protocolo de avaliação descrito no capítulo 3 do TCC. Os comandos abaixo partem de `tutor/`.

**Divisão entre entradas e resultados.** Aqui ficam apenas as **entradas** versionadas do instrumento, lidas pelo código (`exercicios/`, `roteiros/`, `perfis/` e o modelo de manifesto). Os **resultados** (`dados_brutos/`, `dados_processados/`, `resultados/`) ficam em `pesquisa/11_avaliacao/`, fora do Git.

- `exercicios/`: referências e três oráculos `oraculo.v1.json` com testes ocultos, mutantes e hashes;
- `roteiros/`: 15 casos Replay preservados para qualificação pré-campanha;
- `perfis/`: cinco perfis (cenários dirigidos por risco), com fases, intenções e marcadores versionados para a campanha reativa;
- `pesquisa/11_avaliacao/dados_brutos/`: eventos JSONL por execução, ignorados pelo Git;
- `pesquisa/11_avaliacao/dados_processados/`: resumos, matriz requisito–episódio e ficha de codificação;
- `pesquisa/11_avaliacao/resultados/`: relatório Markdown da qualificação ou da campanha definitiva, identificado pelo `run_id`.

E01 (paridade), E02 (média dos valores estritamente positivos) e E03 (maior valor) pertencem ao aluno simulado, não ao tutor. O tutor é o chat livre de introdução à programação: não recebe exercício, episódio nem perfil, e só conhece o exercício pelo que o aluno escreve. No turno 1, o harness insere o enunciado integral antes da fala do perfil; código vai sempre dentro do texto. Os enunciados públicos explicitam Python, a assinatura da função e o domínio de entrada.

O escopo do tutor é a ementa de Programação I do PPC de Engenharia de Software da UPE Garanhuns (`src/tutor_socratico/sela/nivel0/dominio/escopo_cs1.v1.json`). Uma fala fora dele gera o status `ENTRADA_FORA_DO_ESCOPO`, contado à parte dos erros técnicos. Cada episódio tem uma sondagem de até dez turnos: o aluno simulado encerra a conversa quando a sondagem fecha.

Com a imagem `passo-python-runner:latest` disponível no Docker, a qualificação offline é executada com `PYTHONPATH=src python scripts/executar_qualificacao.py`. O comando valida as três referências e os nove mutantes e percorre os 15 casos com `ReplayAdapter` estrito. Seus dados são de **qualificação pré-campanha**, separados do piloto e da campanha.

Com `PYTHONPATH=src`, `python scripts/executar_alunos_simulados.py plan --phase campaign` enumera os 45 IDs sem rede. Para o piloto e a campanha, inicie uma instância dedicada da API com DeepSeek V3.2/SiliconFlow e configure `OPENROUTER_API_KEY` e `TUTOR_RESEARCH_TOKEN` no ambiente local. O executor usa a API HTTP do frontend, enquanto o Qwen recebe só enunciado e diálogo público. `run --phase pilot --run-id <id> --execute` produz cinco episódios; `analyse --run-id <id>` gera matriz, ficha por episódio e ficha por turno para revisão manual. Na fidelidade, preencha `aderente`, `desvio` ou `indeterminado` por fase (abertura, pressão, reação, desfecho); os demais marcadores aceitam `sim`, `não`, `indeterminado` ou `não se aplica` sem oportunidade observável. Todas as decisões exigem trecho justificativo. Para cada `CLOSING_RESOLVED`, codifique suficiência da evidência e contradição pendente; não marque ambos como `sim`. O episódio agrega somente fases observadas; encerramento antecipado não conta fases posteriores. `finalize --run-id <id>` produz a matriz e o resumo finais sem apagar as fichas. Depois da revisão de aderência do piloto, `freeze --pilot-run-id <id> --review <arquivo.json> --output <manifesto.json>` exige commit limpo e congela hashes, escopo curricular, versão dos perfis, limite de turnos, ordem e teto de custo (manifesto 1.3.0). A campanha exige `run --phase campaign --run-id <id> --manifest <manifesto.json> --execute`.

O arquivo de revisão piloto contém `approved_episode_ids` com os cinco IDs `PILOTO` somente após a leitura dos diálogos e um objeto `pricing_snapshot` com `checked_at`, `source_url`, `tutor` e `student` (preços consultados para entrada e saída de cada modelo). Essa aprovação significa aptidão do instrumento, não julgamento de aprendizagem. Não inclua credenciais no arquivo. A campanha não é iniciada automaticamente pelo comando de análise ou congelamento.

O manifesto registra os dois modelos e suas rotas, parâmetros, privacidade, custo observado, commit e hashes, sem valores de credenciais. A pesquisa não recruta participantes; a ficha é codificação autoral de diálogos artificiais. Revise qualquer transcrição antes de divulgação externa.

O ensaio técnico usa `plan --phase rehearsal` e, depois dos preflights e da confirmação do limite externo de gasto da conta OpenRouter, `run --phase rehearsal --run-id <id-inédito> --execute`. Ele produz cinco episódios, um por perfil, nas mesmas combinações de exercício do piloto, mas com IDs `ENSAIO` distintos e conversa nova a cada episódio. Não integra o piloto nem a campanha e não aceita manifesto. `analyse` cria 50 verificações e uma proposta JSON explicitamente provisória. O autor revisa transcrições, julgamentos e fichas antes de `finalize`. Uma falha permanece inconclusiva no mesmo `run_id`; eventual repetição recebe outro ID.
