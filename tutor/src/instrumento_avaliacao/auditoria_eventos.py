"""Auditoria dos eventos de agente gravados: chamadas consumidas e metadados completos."""

from tutor_socratico.contratos.politica_correcao import correction_rule_allowed

# Componentes SELA cujas chamadas o instrumento audita (protocolo, VT-01 e VT-09).
TUTOR_AGENTS = frozenset({"N0-A", "N0-B", "N2", "N1-G", "N1-V"})


def accepted_agent_events(events):
    """Return consumed calls and validate blocked correction pairs (legacy calls retained)."""
    accepted = []
    valid = True
    counts = {}
    pending = None
    for event in events:
        if event.get("agent") not in TUTOR_AGENTS:
            if pending is not None:
                valid = False
            continue
        key = (event.get("conversation_id"), event.get("turn"), event["agent"])
        if "attempt_index" in event:
            counts[key] = counts.get(key, 0) + 1
            valid = valid and event["attempt_index"] == counts[key]
        if event.get("status") == "retried":
            # Tentativa cobrada e cortada por limite de tokens (ADR 0018): conta no índice e no custo,
            # mas não é chamada consumida nem interrompe o par de correção.
            valid = valid and event.get("error_kind") == "structured_json_truncado" and bool(event.get("generation_id"))
            continue
        if event.get("status") == "rejected":
            valid = valid and pending is None and event.get("blocked") is True
            valid = valid and correction_rule_allowed(event["agent"], event.get("error_kind"), event.get("correction_policy_version"))
            valid = valid and event.get("correction_index") == 0 and bool(event.get("output_sha256"))
            pending = event
            continue
        valid = valid and event.get("status") == "ok"
        if pending is not None:
            prior_key = (pending.get("conversation_id"), pending.get("turn"), pending["agent"])
            valid = valid and key == prior_key and event.get("correction_index") == 1
            valid = valid and event.get("correction_of") == pending.get("output_sha256")
            valid = valid and event.get("correction_policy_version") == pending.get("correction_policy_version")
            pending = None
        else:
            valid = valid and not event.get("correction_index") and not event.get("correction_of")
        accepted.append(event)
    return accepted, valid and pending is None


def tutor_metadata_complete(events, costs=None):
    """Real model attempts require linkage; deterministic policy has no provider cost."""
    for event in events:
        if event.get("agent") not in TUTOR_AGENTS or event.get("source") == "deterministic_policy":
            continue
        # Legacy records keep their original interpretation; current calls are strict.
        if "attempt_index" not in event:
            continue
        generation_id = event.get("generation_id")
        usage = event.get("usage") or {}
        if not generation_id or not isinstance(usage.get("total_tokens"), int) or usage["total_tokens"] <= 0:
            return False
        if costs is not None and costs.get(generation_id) is None:
            return False
    return True
