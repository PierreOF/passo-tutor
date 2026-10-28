def media_notas_positivas(notas: list[float]) -> float | None:
    validas = [nota for nota in notas if nota > 0]
    return sum(validas) / len(validas) if validas else None
