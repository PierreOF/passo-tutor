# Passo — protótipo frontend

Interface web do tutor socrático para estudantes de programação introdutória. O estudante pode iniciar uma conversa livre ou praticar pela lista de exercícios. A preferência entre tema claro e escuro fica salva no navegador.

Fora dos testes, a interface usa a API Python com autenticação. O modo simulado fica disponível em testes ou quando `VITE_TUTOR_GATEWAY=mock` for selecionado explicitamente. A API chama o provedor de LLM quando uma chave válida está configurada.

## Executar

```bash
cd tutor/frontend   # a partir da raiz do repositório
npm install
npm run dev
```

Abra `http://127.0.0.1:5173`. Crie o administrador e execute o seed dos cinco alunos seguindo `../docs/desenvolvimento/operacao_mvp.md`; os comandos imprimem credenciais temporárias na primeira criação.

Para habilitar a API e a execução das tentativas, execute em `tutor/` (ou use `docker compose -f tutor/compose.yaml ...` a partir da raiz do repositório):

```bash
docker build -t passo-python-runner:latest ./executor
docker compose up -d --build
```

O proxy de desenvolvimento encaminha `/api` para `127.0.0.1:8000`. A chave `OPENROUTER_API_KEY` fica somente no ambiente ou `.env` de `tutor/`. Alunos veem o próprio histórico; administradores acompanham conversas e perfis em `/admin`.

O executor usa Docker sem rede e com limites de recursos. Os testes de cada atividade permanecem na API e não são enviados ao navegador.

## Validar

```bash
npm run lint
npm test
npm run build
```

## Fronteira de integração

As páginas dependem do contrato `TutorGateway` em `src/services/tutorGateway.ts`. `MockTutorGateway` mantém a demonstração determinística. `HttpTutorGateway` conecta a interface à API Python e é o padrão fora dos testes; selecione `VITE_TUTOR_GATEWAY=mock` explicitamente para navegação visual sem chamadas à API.

O navegador nunca chama o OpenRouter e nunca recebe credenciais do provedor. Modelo, ZDR, negação de coleta e parâmetros são definidos no backend. `frontend/.env.example` contém apenas a seleção pública do gateway.

O proxy e os endpoints de sessão/turno podem ser conferidos sem LLM por `python scripts/smoke_frontend_backend.py` a partir de `tutor/`. O script usa Replay nas portas locais 8001 e 5174, sem interromper o Compose, e encerra os servidores de smoke ao terminar. `VITE_API_TARGET` permite apontar o proxy de desenvolvimento para outra porta somente nesse smoke.

O conteúdo técnico interno do Solucionador, prompts internos e respostas rejeitadas não integram a interface do estudante nem os registros exportáveis deste protótipo.
