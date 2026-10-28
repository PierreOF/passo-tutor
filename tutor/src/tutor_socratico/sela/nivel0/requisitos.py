"""Regra estrutural dos requisitos de resolução fixados por N0-B (contrato 4.0.0)."""

from __future__ import annotations

from ...contratos.esquemas import MAX_REQUIREMENTS
from ...contratos.modelos import DiagnosisContract
from ...infraestrutura.llm import AdapterError


def validate_requirements(diagnosis: DiagnosisContract) -> None:
    """N0-B 4.0.0 exige R01..Rn contíguos e únicos, de 1 a 3 itens."""
    ids = [item.requirement_id for item in diagnosis.resolution_requirements]
    if not 1 <= len(ids) <= MAX_REQUIREMENTS or ids != [f"R{index:02d}" for index in range(1, len(ids) + 1)]:
        raise AdapterError("n0b_requisitos_invalidos")
    for item in diagnosis.resolution_requirements:
        if not item.description.strip():
            raise AdapterError("n0b_requisitos_invalidos")
