# Diagramas do tutor e da avaliação

Todos os diagramas estão em draw.io. Cada `.drawio` é a fonte editável, e o PNG ao lado é a exportação incorporada no [README do tutor](../../../README.md). Os HTMLs gerados em 21/10/2026 com o skill diagram-design (módulos, fluxo SELA e orquestração da avaliação) foram removidos em 25/10/2026; o conteúdo deles passou para os diagramas abaixo.

Arquitetura em níveis, do mais geral ao mais detalhado:

- [Visão geral](tutor-visao-geral.drawio) ([PNG](tutor-visao-geral.png)): usuários, sistema avaliado, instrumento de avaliação e provedor de modelos.
- [Componentes implementados](tutor-arquitetura.drawio) ([PNG](tutor-arquitetura.png)): frontend, backend com rotas, fila, concorrência e núcleo SELA, PostgreSQL, instrumento (o executor de código só valida os oráculos na qualificação Replay) e modelos. Substitui o antigo diagrama de módulos.
- [SELA multiagente](tutor-sela-multiagente.drawio) ([PNG](tutor-sela-multiagente.png)): os cinco agentes, com o que cada um recebe, produz e não pode fazer, o orquestrador, as guardas, a barreira e o estado da sessão compartilhado.
- [Camadas da SELA](tutor-sela-camadas.drawio) ([PNG](tutor-sela-camadas.png)): responsabilidades dos níveis 0, 2 e 1, contrato imutável, guarda de resolução, regras da barreira e desfechos de um turno. Substitui o antigo fluxo SELA.
- [Campanha](avaliacao-campanha.drawio) ([PNG](avaliacao-campanha.png)): o harness alterna aluno simulado e tutor, registra os episódios e os entrega aos verificadores e aos juízes. Substitui a antiga orquestração da avaliação.
- [Juízes](juizes-avaliacao.drawio) ([PNG](juizes-avaliacao.png)): qualificação por controles e gate por juiz; julgamento em duas raias paralelas, uma para o juiz A (GLM-5.3 via AkashML) e outra para o juiz B (DeepSeek V4.1 Flash via Together), com o mesmo prompt, a mesma rubrica, temperatura 0 e as mesmas duas chamadas, sem que um veja o parecer do outro; e consolidação pela Decisão 11 (acordo A × B, entrega da solução decidida só pelo C1 (não revela a solução) dos dois juízes: os dois dizem que vazou, sim; os dois dizem que não vazou, não; divergência ou parecer inválido, inconclusivo). A campanha não tem verificação objetiva de vazamento.

## Logotipos e ícones

Os logotipos de ferramentas são do projeto Simple Icons (versões 13.21.0 e 15.22.0, domínio público CC0 1.0), e o ícone de monitor vem da biblioteca do draw.io. Os logotipos dos modelos (Qwen para o tutor, MiniMax para o aluno simulado, Zhipu para o juiz A com o GLM-5.3 e DeepSeek para o juiz B) são do LobeHub Icons, versão 1.99.0 (`@lobehub/icons-static-svg`, licença MIT). As marcas pertencem aos seus titulares. Os ícones de papel (robô para agente LLM, pessoa e administrador com escudo, escudo, lista, cadeado, camadas para os níveis, banco de dados para estado e episódios gravados, e outros) foram desenhados para estes diagramas como SVG de traço 24 × 24 e estão embutidos nos próprios `.drawio`; a legenda e as cores estão no README do tutor. Para reaproveitar um ícone em outro diagrama, copie a célula no draw.io.

## Manutenção e exportação

Edite o `.drawio` no draw.io e exporte o PNG com escala 2 e borda 10, por exemplo:

```powershell
docker run --rm -v "<pasta com os .drawio>:/data" rlespinasse/drawio-export:v4.38.0 --format png --scale 2 --border 10
```

O exportador grava em `<pasta>/export/<nome>-<página>.png`; renomeie para `<nome>.png` e inspecione o resultado (cortes, setas cruzando caixas, rótulos sobrepostos).

Ao mudar comportamento ou fronteiras, confira o código correspondente e reexporte o PNG: `sela/orquestrador.py`, `sela/conversa.py` e `sela/nivel1/barreira.py` (camadas e multiagente); `api/servicos.py` (componentes); `campanha/execucao.py` e `campanha/verificadores.py` (campanha); `juiz/qualificacao_juizes.py`, `juiz/chamada.py`, `juiz/comparacao.py` e `juiz/resultado.py` (juízes).

As setas representam relações principais, sem enumerar todos os retornos HTTP ou dependências Python. A/B aparecem juntos, mas seus pareceres são separados. Os diagramas não afirmam que uma campanha foi executada ou que os gates estão aprovados, e não substituem as fontes normativas da SELA nem o protocolo de avaliação do TCC.
