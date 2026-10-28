# Operação do MVP

## Configuração

### Contas e banco de dados

Os comandos desta página partem do diretório `tutor/` (`cd tutor`), onde ficam `compose.yaml`, `.env`, `executor/`, `src/` e `tests/`; a partir da raiz do repositório, use `docker compose -f tutor/compose.yaml ...`. Os resultados da avaliação ficam em `pesquisa/11_avaliacao/`.

Todos os ambientes usam PostgreSQL. Se `.env` ainda nao existir, copie `.env.example` para `.env`, troque `POSTGRES_PASSWORD` por uma senha forte e confira `DATABASE_URL` (banco `passo`) e `TEST_DATABASE_URL` (banco `passo_test`). O `.env` contem credenciais e nao e versionado. Inicie o banco local antes da API:

O Compose inicia PostgreSQL e backend. Antes da primeira subida, construa a imagem isolada `passo-python-runner`, usada pelos oráculos do instrumento (testes ocultos e qualificação Replay) para executar código sem rede. As URLs do `.env` apontam para `127.0.0.1:55432` para comandos executados no host; o backend usa automaticamente `postgres:5432` dentro da rede do Compose.

```powershell
docker build -t passo-python-runner:latest ./executor
docker compose up -d --build
docker compose ps
docker compose run --rm backend python -m tutor_socratico.api create-admin
docker compose run --rm backend python -m tutor_socratico.api seed-default-students
```

O comando de criação do administrador é interativo e deve ser executado uma vez. O frontend permanece fora do Docker: em outro terminal PowerShell, execute `npm.cmd --prefix frontend install` e `npm.cmd --prefix frontend run dev`; acesse `http://127.0.0.1:5173`. O proxy do Vite encaminha `/api` para a porta local `8000`. O serviço HTTP `executor` foi removido em 22/10/2026 (nenhuma rota o chamava); a execução de código ficou só no instrumento, que inicia no host um contêiner sem rede por tentativa, com limites de CPU, memória e processos.

Para verificar e investigar erros sem chamar o OpenRouter:

```powershell
curl.exe http://127.0.0.1:8000/api/health
docker compose logs --tail=100 backend postgres
docker compose run --rm -T backend python -m unittest discover -s tests -v
```

Erros HTTP 5xx produzem um `requestId` na resposta e uma linha `api_error` nos logs do backend com o mesmo identificador. Falhas do provedor registram apenas tipo normalizado, status HTTP e número de tentativas, sem chave ou conteúdo do prompt. A rota `/api/health` confere o banco, sem chamada paga. Para parar, use `docker compose down` sem `-v`, preservando o volume do PostgreSQL.

O seed gera os códigos `aluno01` a `aluno05`, cada qual com senha aleatória temporária e troca obrigatória no primeiro acesso. As senhas são impressas apenas quando os registros são criados; guarde a saída em local restrito. Rodar o comando novamente não redefine credenciais. Administradores autenticados podem criar novos perfis, redefinir senhas e consultar conversas em `/admin`. Cada aluno vê somente o próprio histórico em `/historico`.

As sessões autenticadas usam cookie `HttpOnly`; chamadas mutáveis exigem token CSRF. Configure HTTPS na implantação e mantenha a sessão inacessível a scripts do navegador. As execuções da campanha usam token de serviço separado e não são listadas como conversas de alunos.

Revogue qualquer chave já publicada em conversa, documento ou histórico. Crie uma chave exclusiva do projeto no OpenRouter, com limite de gasto e expiração, copie `.env.example` para `.env` e preencha somente `OPENROUTER_API_KEY`. O arquivo `.env` é local e ignorado pelo Git.

O padrão de desenvolvimento no código ainda é `openai/gpt-4o`. Os modelos da avaliação vêm do registro único `tutor/avaliacao/modelos.v1.json` (tutor, aluno simulado, juízes A e B, fixados pela regra do ranking da Revisão 3). Antes do piloto, para os quatro papéis:

1. Rode a descoberta gratuita (a partir de `tutor/`): `PYTHONPATH=src python scripts/preflight_modelos.py descobrir`. Ela imprime, por endpoint, `provider_slug` (prefixo da tag, a rota) e `provider_nome` (nome de exibição do provedor, ex.: `Z.AI`), além de preço, quantização, parâmetros e elegibilidade ZDR.
2. Rode o teste técnico pago de cada papel na rota escolhida: `PYTHONPATH=src python scripts/preflight_modelos.py testar --papel <tutor|aluno|juiz_A|juiz_B> --provider <slug> [--provider-nome <nome>] --execute --cost-cap-usd X`. O relatório (só metadados) fica em `pesquisa/11_avaliacao/dados_brutos/preflight_modelos_<papel>_<UTC>.json`.
3. Preencha `provider_slug` e `provider_nome` dos quatro papéis no registro antes do piloto. A conferência de custos aceita o `provider_name` de `/generation` igual, após normalização (minúsculas, só letras e dígitos), ao slug ou ao `provider_nome`; só o provedor é fixado, não a quantização.
4. No `.env`, defina `TUTOR_LLM_MODEL`, `TUTOR_LLM_PROVIDER_SLUG`, `TUTOR_LLM_TEMPERATURE`, `TUTOR_LLM_MAX_TOKENS` e `TUTOR_LLM_REASONING_EFFORT` iguais ao papel `tutor` do registro, com `TUTOR_LLM_ALLOW_PROVIDER_FALLBACKS=false`. Valores vigentes (tutor na rota SiliconFlow por exceção; aluno e juízes na Together): `TUTOR_LLM_MODEL=qwen/qwen3.8-2.4t-a95b`, `TUTOR_LLM_PROVIDER_SLUG=siliconflow`, `TUTOR_LLM_TEMPERATURE=0.0`, `TUTOR_LLM_MAX_TOKENS=6000`, `TUTOR_LLM_TIMEOUT_SECONDS=150` e `TUTOR_LLM_REASONING_EFFORT=low`. `TUTOR_LLM_REASONING_EFFORT` aceita `minimal`, `low`, `medium` ou `high` e é enviado como `reasoning.effort`; vazio não envia o campo. A campanha recusa o backend se modelo, rota, temperatura, limite de tokens ou esforço de raciocínio divergirem do registro. Aluno simulado e juízes recebem o esforço do próprio papel no registro (hoje `low` nos quatro; aluno e juízes na rota Together).

Registre modelo solicitado e retornado, provedor efetivo, temperatura, commit e hashes no manifesto e não altere a configuração durante as execuções reais. As flags `TUTOR_LLM_ZDR=true` e `TUTOR_LLM_DATA_COLLECTION=deny` são obrigatórias. A configuração anterior (`deepseek/deepseek-v3.2` na rota `siliconflow`, decidida em `pesquisa/08_metodologia/decisao_modelo_pre_campanha_2026-09-24.md`) é registro histórico e não vale para as próximas execuções.

O frontend usa `frontend/.env` apenas para `VITE_TUTOR_GATEWAY=http`. Nunca crie variável `VITE_*` com credencial: tudo que possui esse prefixo é público no navegador.

### Julgamento da campanha, relatório e reavaliação

Os comandos vêm de `scripts/executar_alunos_simulados.py` (a partir de `tutor/`, com `PYTHONPATH=src`). O gate reservado aprovado é exigido por `judge-run`.

1. `judge-plan --run-id <id>`: conta os pacotes (sem rede). `judge-run --run-id <id> --cost-cap-usd X --execute`: julgamento pago da campanha, com teto próprio.
2. `judge-compare --run-id <id>`: gera o `resultado.json`/`.md` dos juízes (sem rede). Rode-o de novo sempre que os pareceres mudarem: o relatório recusa um resultado em formato antigo.
3. `python scripts/graficos_juizes.py --run-id <id> [--rotulo <r>] [--gate <pasta>]`: gera o PDF final (resumo, quadro por tipo de aluno, entrega da solução com exemplo real, confiança nos juízes, apêndice e glossário) a partir do resultado regenerado no passo 2. Falha de forma explícita com um resultado antigo.
4. `judge-run --run-id <id> --reavaliacao --cost-cap-usd X --execute`: etapa paga, com teto próprio. Julga de novo 9 conversas conformes (ordenadas por repetição, exercício e perfil) em `juizes_reavaliacao/`, sem tocar `juizes/`; o rótulo `reavaliacao` é reservado a esta flag. O apêndice usa a pasta se existir e se a versão da rubrica for a mesma dos pareceres originais (se diferir, o bloco fica fora com um aviso). Depois da reavaliação, rode o passo 3 de novo para incluir o apêndice.
5. Quando um novo gate reservado for aprovado, atualize `HISTORICO_TESTE_PREVIO` em `scripts/graficos_juizes.py` (semente a semente: aprovada ou reprovada e o motivo); o relatório lê esse histórico pela semente do gate.

## Logs

Para cada turno, registrar: identificador de execução, identificador de conversa, versão da configuração, entrada do estudante, estado antes/depois, nível permitido, nível candidato, decisão do Sanitizador, resposta final, latência, uso de tokens e erro normalizado.

Não registrar chaves, tokens de autenticação, prompts internos completos, saída do Solucionador ou dados pessoais desnecessários. Logs publicáveis devem ser anonimizados e revisados antes de sair de `pesquisa/11_avaliacao/dados_brutos/`.

## Retenção

Enquanto a avaliação usa apenas roteiros simulados, os logs podem ser mantidos no projeto. Qualquer coleta futura com pessoas requer termo, política de retenção, anonimização e autorização ética aplicável antes do registro.

## Execução

Para executar testes, defina `TEST_DATABASE_URL` para o banco `passo_test`; os testes limpam esse banco dedicado antes de cada caso e recusam URLs que apontem para outro banco. Para iniciar a integracao local:

```powershell
docker compose up -d --build
```

Em outro terminal, execute `npm --prefix frontend run dev` (a partir de `tutor/`); o gateway HTTP é o padrão. Sem chave, `/api/health/llm` responde `configured=false` e os endpoints do tutor falham de modo seguro.

As mensagens visíveis (inclusive o código colado na fala do aluno), exercício e timestamps são persistidos para o histórico. Não são gravados prompts internos nem candidatos recusados. Acesse dados e backups somente com controle restrito; defina retenção antes de qualquer coleta com pessoas.

O teste real nunca integra a suíte padrão. O preflight manual pago de chave e rota é o `testar` de `scripts/preflight_modelos.py` (passo 2 acima), que só chama o provedor com `--execute` e teto de custo explícito e grava apenas metadados:

```powershell
$env:PYTHONPATH='src'; python scripts/preflight_modelos.py testar --papel tutor --provider <slug> --execute --cost-cap-usd X
```

O preflight de seleção dos cinco contratos é separado da campanha e usa solicitações sintéticas. A verificação vigente dos modelos é o `testar` de `scripts/preflight_modelos.py` descrito acima, que reutiliza essas sondas para o tutor; `scripts/preflight_selecao_modelo.py` com `--model deepseek/deepseek-v3.2 --provider-slug siliconflow` documenta a seleção histórica de 24/09/2026. Os scripts gravam somente metadados, sem prompts ou respostas. Não executar os casos reais antes dos gates do manifesto.
