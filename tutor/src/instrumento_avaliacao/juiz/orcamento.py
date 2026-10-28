"""Teto de custo dos comandos pagos dos juízes (custo por `generation_id`, como `campanha/custos.py`)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

from ..campanha.custos import COST_DEFERRED_WAITS_SECONDS

# Esperas dos juízes: as da campanha mais 3 e 5 minutos, porque um custo que o OpenRouter demora a
# publicar interrompia o gate (qualificacao_juizes_20261022T192948Z) e queimava o conjunto reservado.
ESPERAS_CUSTO_JUIZ = (*COST_DEFERRED_WAITS_SECONDS, 180, 300)


class Orcamento:
    """Acumula o custo de cada parecer e diz quando parar.

    Para quando o custo acumulado atinge o teto (`teto_atingido`) ou quando o custo de uma
    geração não pôde ser aferido (`custo_indisponivel`): sem o custo, o teto não é garantido.
    Chamadas que falharam sem `generation_id` não têm custo consultável e não entram na soma.
    O teto é conferido depois de cada parecer e, esgotado o orçamento, nenhum par novo começa: em sequência
    (a qualificação, `judge-qualify`) o teto pode ser excedido em até um parecer (duas chamadas); em paralelo
    (o `judge-run`) em até 10 pareceres, os que estavam em andamento.
    `registrar` e `esgotado` são seguros entre threads: a consulta de custo (que pode dormir) roda
    fora da trava; só a soma e a definição de `motivo_parada` ficam sob ela.
    """

    def __init__(
        self, teto_usd: float, custo: Callable[[str], float | None], *,
        esperas: tuple[float, ...] = ESPERAS_CUSTO_JUIZ, dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        if not teto_usd > 0:
            raise ValueError("o teto de custo deve ser positivo")
        self.teto_usd = float(teto_usd)
        self._custo = custo
        self._esperas = tuple(esperas)
        self._dormir = dormir
        self.gasto_usd = 0.0
        self.motivo_parada: str | None = None
        self._trava = threading.Lock()

    def registrar(self, parecer: Mapping[str, Any]) -> dict[str, float | None]:
        """Consulta o custo das gerações do parecer, soma ao gasto e devolve `{generation_id: custo}`."""
        custos: dict[str, float | None] = {}
        for chamada in parecer.get("chamadas") or []:
            generation_id = chamada.get("generation_id")
            if not generation_id:
                continue
            custo = self._custo_com_espera(generation_id)
            custos[generation_id] = custo
            with self._trava:
                if custo is None:
                    self.motivo_parada = "custo_indisponivel"
                else:
                    self.gasto_usd = round(self.gasto_usd + float(custo), 8)
        with self._trava:
            if self.motivo_parada is None and self.gasto_usd >= self.teto_usd:
                self.motivo_parada = "teto_atingido"
        return custos

    def _custo_com_espera(self, generation_id: str) -> float | None:
        """Consulta o custo; se ainda indisponível, repete após cada espera diferida da campanha."""
        custo = self._custo(generation_id)
        for espera in self._esperas:
            if custo is not None:
                break
            self._dormir(espera)
            custo = self._custo(generation_id)
        return custo

    def parar(self, motivo: str) -> None:
        """Marca a parada (se ainda não houver uma), sob a trava; ex.: `custo_indisponivel` quando um par falha
        depois de chamadas que podem ter sido pagas e cujo custo não entrou na soma."""
        with self._trava:
            if self.motivo_parada is None:
                self.motivo_parada = motivo

    @property
    def esgotado(self) -> bool:
        with self._trava:
            return self.motivo_parada is not None
