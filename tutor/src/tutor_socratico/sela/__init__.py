"""Materialização multiagente da SELA: níveis 0, 1 e 2, coordenados pelo orquestrador.

Os níveis não são agentes. N0 (`nivel0`) delimita o escopo e fixa o contrato,
N1 (`nivel1`) gera, valida e barra a pergunta, N2 (`nivel2`) planeja e atualiza o
estado. O orquestrador coordena a ordem N0 -> N2 -> N1 sem absorver nenhum nível.
"""
