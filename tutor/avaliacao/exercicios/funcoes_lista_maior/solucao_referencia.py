def maior_valor(valores: list[int]) -> int:
    maior = valores[0]
    for valor in valores[1:]:
        if valor > maior:
            maior = valor
    return maior
