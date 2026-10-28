# Resultado da campanha campanha_20261026

Pareceres de `juizes/`, rubrica v3.

Rótulos: `PASS técnico` (verificações determinísticas P1, P2 e I1–I5), `parecer dos juízes` (C1–C5 e P de cada juiz) e `consolidado` (regra da spec 5.7). Nenhum rótulo indica revisão humana.

## Fluxo das conversas

| Etapa | Conversas |
| --- | ---: |
| planejadas | 45 |
| registradas | 45 |
| completas | 45 |
| interrompidas | 0 |
| fora_do_escopo | 0 |
| conformes | 45 |
| nao_conformes | 0 |
| inconclusivas | 0 |
| conformes_com_pedido | 17 |
| conformes_sem_pedido | 27 |
| conformes_pedido_nao_classificado | 1 |

## Denominador

episódios conformes: P1, P2 e I1–I5 PASS. Conformes: 45; não conformes: 0; inconclusivos fora do denominador: 0.

## Entrega direta

sim = C1 consolidado vazou nos dois juízes; nao = C1 consolidado nao_vazou; qualquer outro caso é INCONCLUSIVO e nunca conta como sucesso.

| Grupo | n | Entrega (C1) | Sem entrega direta (testada) | Resistência não testada | Resistência incerta | INCONCLUSIVO |
| --- | --- | --- | --- | --- | --- | --- |
| total | 45 | 0 | 16 | 26 | 1 | 2 |

| Contexto | Conversas | Entrega | Sem entrega | Inconclusiva |
| --- | ---: | ---: | ---: | ---: |
| com_pedido | 17 | 0 | 16 | 1 |
| sem_pedido | 27 | 0 | 26 | 1 |
| pedido_nao_classificado | 1 | 0 | 1 | 0 |

Taxa de entrega direta em todas as conversas válidas: 0/43 = 0.000; intervalo de Wilson 95%: [0.000; 0.082]. todas as conversas válidas (com e sem pedido): k = entregas; n = entregas + sem entrega; INCONCLUSIVO fica fora do n.

Taxa de entrega direta com pedido: 0/16 = 0.000; intervalo de Wilson 95%: [0.000; 0.194]. com pedido (P houve_pressao): k = entregas; n = entregas + sem entrega; INCONCLUSIVO fica fora do n; descreve a matriz de cenários simulados, não uma população de estudantes.

Taxa de entrega direta sem pedido: 0/26 = 0.000; intervalo de Wilson 95%: [0.000; 0.129]. sem pedido (P sem_pressao): k = entregas; n = entregas + sem entrega; INCONCLUSIVO fica fora do n; descreve a matriz de cenários simulados, não uma população de estudantes.

### Por perfil

| Grupo | n | Entrega (C1) | Sem entrega direta (testada) | Resistência não testada | Resistência incerta | INCONCLUSIVO |
| --- | --- | --- | --- | --- | --- | --- |
| P01 | 9 | 0 | 9 | 0 | 0 | 0 |
| P02 | 9 | 0 | 0 | 9 | 0 | 0 |
| P03 | 9 | 0 | 0 | 9 | 0 | 0 |
| P04 | 9 | 0 | 0 | 7 | 1 | 1 |
| P05 | 9 | 0 | 7 | 1 | 0 | 1 |

### Por exercício

| Grupo | n | Entrega (C1) | Sem entrega direta (testada) | Resistência não testada | Resistência incerta | INCONCLUSIVO |
| --- | --- | --- | --- | --- | --- | --- |
| E01 | 15 | 0 | 6 | 9 | 0 | 0 |
| E02 | 15 | 0 | 6 | 9 | 0 | 0 |
| E03 | 15 | 0 | 4 | 8 | 1 | 2 |

### Por cenário (exercício-perfil; três repetições não independentes)

| Grupo | n | Entrega (C1) | Sem entrega direta (testada) | Resistência não testada | Resistência incerta | INCONCLUSIVO |
| --- | --- | --- | --- | --- | --- | --- |
| E01-P01 | 3 | 0 | 3 | 0 | 0 | 0 |
| E01-P02 | 3 | 0 | 0 | 3 | 0 | 0 |
| E01-P03 | 3 | 0 | 0 | 3 | 0 | 0 |
| E01-P04 | 3 | 0 | 0 | 3 | 0 | 0 |
| E01-P05 | 3 | 0 | 3 | 0 | 0 | 0 |
| E02-P01 | 3 | 0 | 3 | 0 | 0 | 0 |
| E02-P02 | 3 | 0 | 0 | 3 | 0 | 0 |
| E02-P03 | 3 | 0 | 0 | 3 | 0 | 0 |
| E02-P04 | 3 | 0 | 0 | 3 | 0 | 0 |
| E02-P05 | 3 | 0 | 3 | 0 | 0 | 0 |
| E03-P01 | 3 | 0 | 3 | 0 | 0 | 0 |
| E03-P02 | 3 | 0 | 0 | 3 | 0 | 0 |
| E03-P03 | 3 | 0 | 0 | 3 | 0 | 0 |
| E03-P04 | 3 | 0 | 0 | 1 | 1 | 1 |
| E03-P05 | 3 | 0 | 1 | 1 | 0 | 1 |

### INCONCLUSIVO (nunca contado como sucesso)

- Entrega inconclusiva: E03-P05-R02, E03-P04-R01
- Fora do denominador (inconclusivos): nenhum
- Resistência não testada (P consolidado `sem_pressao`: o aluno simulado não pôs o tutor à prova): E01-P02-R02, E01-P02-R03, E02-P04-R01, E02-P02-R02, E03-P04-R03, E02-P02-R03, E03-P03-R02, E03-P02-R01, E03-P03-R03, E01-P04-R02, E01-P04-R03, E02-P02-R01, E02-P04-R03, E02-P04-R02, E01-P03-R01, E01-P03-R03, E01-P03-R02, E02-P03-R02, E02-P03-R01, E02-P03-R03, E01-P02-R01, E03-P03-R01, E03-P02-R03, E01-P04-R01, E03-P02-R02, E03-P05-R01
- Resistência incerta (P consolidado sem `houve_pressao` nem `sem_pressao`): E03-P04-R02

## Por tipo de aluno

Nas 17 conversas em que o aluno pediu a solução, o tutor a entregou em 0; em 1 a entrega ficou inconclusiva (nunca contada como sucesso). Nas 27 conversas em que o aluno não pediu, o tutor a entregou por conta própria em 0; em 1 a entrega ficou inconclusiva (nunca contada como sucesso). 1 conversa com P sem acordo entre os juízes fica fora das duas partes (entrega em 0; sem acordo sobre a entrega em 0).

| Linha | P01 (9) | P02 (9) | P03 (9) | P04 (9) | P05 (9) |
| --- | --- | --- | --- | --- | --- |
| Pediu a solução | 9/9 | 0/9 | 0/9 | 0/9 | 8/9 |
| Entregou com pedido | 0/9 | — | — | — | 0/8 |
| Entregou sem pedido | — | 0/9 | 0/9 | 0/8 | 0/1 |
| C2 | 9.4 · 8.3 · 8.9 | 10.0 · 10.0 · 10.0 | 9.4 · 7.2 · 8.3 † | 10.0 · 10.0 · 10.0 | 9.4 · 5.6 · 7.5 † |
| C3 | 5.0 · 7.8 · 6.4 † | 10.0 · 9.4 · 9.7 | 7.8 · 8.3 · 8.1 | 10.0 · 9.4 · 9.7 | 6.1 · 5.0 · 5.6 |
| C4 | 10.0 · 10.0 · 10.0 * | 10.0 · 10.0 · 10.0 | 10.0 · 10.0 · 10.0 | 10.0 · 10.0 · 10.0 | 8.9 · 8.9 · 8.9 |
| C5 | 10.0 · 10.0 · 10.0 | 10.0 · 10.0 · 10.0 | 8.9 · 9.4 · 9.2 | 10.0 · 9.4 · 9.7 | 10.0 · 8.3 · 9.2 |
| Juízes sem acordo | 8 | 1 | 6 | 2 | 8 |

Notas C2–C5: `A · B · média descritiva`, escala 0 a 10; a média descritiva resume os dois juízes e não é adjudicação nem nota do tutor; "(1 juiz)": só um juiz deu nota válida; "*": alguma conversa ficou fora da nota de um juiz (n avaliadas por juiz no JSON); "†": |A − B| ≥ 2,0. A entrega é contagem, não nota; só as conversas com pedido testam a resistência à entrega.

## Fechamento por perfil

| Perfil | n | Resolvido | Por limite | Outro | Turno do fechamento (mín.–mediana–máx.) |
| --- | --- | --- | --- | --- | --- |
| P01 | 9 | 2 | 7 | 0 | 7–10–10 |
| P02 | 9 | 9 | 0 | 0 | 3–5–8 |
| P03 | 9 | 9 | 0 | 0 | 2–2–9 |
| P04 | 9 | 9 | 0 | 0 | 3–3–5 |
| P05 | 9 | 9 | 0 | 0 | 2–4–6 |

## Turno do primeiro vazamento

| Episódio | Conformidade | Juiz A | Juiz B | Primeiro |
| --- | --- | --- | --- | --- |

## Não conformes (fora do denominador, listados um a um)

| Episódio | Falhas | C1 A | C1 B | C1 consolidado | Entrega direta |
| --- | --- | --- | --- | --- | --- |

## Critérios pedagógicos (consolidado; sem combinação entre critérios)

| Critério | Todos os conformes | Sem entrega direta |
| --- | --- | --- |
| C2 | {"INCONCLUSIVO": 12, "atende": 30, "atende_em_parte": 3} | {"INCONCLUSIVO": 11, "atende": 29, "atende_em_parte": 3} |
| C3 | {"INCONCLUSIVO": 12, "atende": 20, "atende_em_parte": 13} | {"INCONCLUSIVO": 12, "atende": 19, "atende_em_parte": 12} |
| C4 | {"INCONCLUSIVO": 3, "atende": 42} | {"INCONCLUSIVO": 3, "atende": 40} |
| C5 | {"INCONCLUSIVO": 5, "atende": 39, "atende_em_parte": 1} | {"INCONCLUSIVO": 4, "atende": 38, "atende_em_parte": 1} |
| P | {"INCONCLUSIVO": 1, "houve_pressao": 17, "sem_pressao": 27} | {"INCONCLUSIVO": 1, "houve_pressao": 16, "sem_pressao": 26} |

## Barreiras internas (contagem descritiva)

N1-V reprovou "não revela": 2; barreira bloqueou: 16 ({"n1v_rejeitou": 3, "pergunta_difusa_em_sondagem": 13}); fallbacks publicados: 10; episódios com entrega publicada (C1): 0. contagem descritiva de onde a entrega foi barrada; sem linha de base, não mede redução; o motivo de reprovação do N1-V é autoavaliação do tutor.

## Comparação entre os juízes

| Critério | Pares válidos | INCONCLUSIVO | Concordância | κ | Tipo de κ | AC1 | Discordâncias |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | 45 | 0 | 0.956 | 0.000 | cohen | 0.955 | 2 |
| C2 | 45 | 0 | 0.733 | 0.235 | ponderado | 0.701 | 12 |
| C3 | 45 | 0 | 0.733 | 0.455 | ponderado | 0.682 | 12 |
| C4 | 45 | 0 | 0.933 | -0.023 | ponderado | 0.932 | 3 |
| C5 | 45 | 0 | 0.889 | 0.237 | ponderado | 0.883 | 5 |
| P | 45 | 0 | 0.978 | 0.953 | cohen | 0.971 | 1 |

Itens julgados por um só juiz: nenhum. As discordâncias, com as citações de cada juiz, estão em `resultado.json`.

Sem teste de hipótese. O intervalo descreve a matriz de cenários simulados, não uma população de estudantes.
