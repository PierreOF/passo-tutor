"""Versioned semantic correction policy, independent of transport retries."""

# Campos de texto obrigatorios (agente, campo) cujo valor vazio vira schema_<campo>_invalido (politica 1.8.0+).
# Campos fechados por enum (gap_type, gap_domain_type, difficulty_type, comprehension_level, socratic_function)
# nao entram: nao podem chegar vazios.
REQUIRED_TEXT_FIELDS = (
    ("N0-A", "justification"),
    ("N0-B", "claim_text"), ("N0-B", "gap_explanation"), ("N0-B", "resolution_criterion"), ("N0-B", "claim_quote"),
    ("N1-G", "text"), ("N1-G", "policy_reason"),
    ("N1-V", "displayed_text"), ("N1-V", "reason"),
    ("N2", "concept_in_doubt"), ("N2", "plan"),
)

TUTOR_CORRECTION_POLICIES = {
    "1.0.0": {
        "version": "1.0.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
    },
    "1.1.0": {
        "version": "1.1.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
    },
    "1.2.0": {
        "version": "1.2.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
        "rule_specific_feedback": True,
    },
    "1.3.0": {
        "version": "1.3.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta",
                            "n0b_requisito_codigo_sem_canal"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
        "rule_specific_feedback": True,
    },
    "1.4.0": {
        "version": "1.4.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta",
                            "n0b_requisito_codigo_sem_canal", "n0b_requisito_nao_demonstravel"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
        "rule_specific_feedback": True,
    },
    "1.5.0": {
        "version": "1.5.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta",
                            "n0b_requisito_nao_demonstravel"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
        "rule_specific_feedback": True,
    },
    "1.6.0": {
        "version": "1.6.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta",
                            "n0b_requisito_nao_demonstravel", "n0b_claim_sem_ancoragem",
                            "n1v_criterios_incoerentes"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
        "rule_specific_feedback": True,
    },
    "1.7.0": {
        "version": "1.7.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta",
                            "n0b_requisito_nao_demonstravel", "n0b_claim_sem_ancoragem",
                            "n1v_criterios_incoerentes", "n1v_resolution_check_incoerente",
                            "n2_contrato_requisitos_inconsistente"],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
        "rule_specific_feedback": True,
    },
    "1.8.0": {
        "version": "1.8.0",
        "max_corrections_per_agent_turn": 1,
        "retryable_rules": ["n0b_criterio_codigo_inconsistente", "n2_redigiu_pergunta", "n2_resolucao_incompleta",
                            "n0b_requisito_nao_demonstravel", "n0b_claim_sem_ancoragem",
                            "n1v_criterios_incoerentes", "n1v_resolution_check_incoerente",
                            "n2_contrato_requisitos_inconsistente",
                            *(f"schema_{key}_invalido" for key in sorted({k for _, k in REQUIRED_TEXT_FIELDS}))],
        "reuse_original_inputs": True,
        "include_rejected_content": False,
        "rule_specific_feedback": True,
    },
}
# 1.9.0 (ADR 0019): mesmas regras da 1.8.0; a correção de citação ou turno recusados do N2 ganha a
# instrução fixa de copiar as palavras do estudante do turno indicado (QUOTE_COPY_FEEDBACK).
TUTOR_CORRECTION_POLICIES["1.9.0"] = {
    **TUTOR_CORRECTION_POLICIES["1.8.0"],
    "version": "1.9.0",
    "retryable_rules": list(TUTOR_CORRECTION_POLICIES["1.8.0"]["retryable_rules"]),
    "quote_copy_feedback": True,
}
TUTOR_CORRECTION_POLICY = TUTOR_CORRECTION_POLICIES["1.9.0"]

# Frases fixas por regra da guarda N2 (política 1.2.0); nunca incluem conteúdo da saída recusada.
GUARD_RULE_FEEDBACK = {
    "reason_vazio": "O item {requirement} tem reason vazio; justifique cada avaliação.",
    "satisfied_sem_evidencia": "O item {requirement} foi marcado satisfied sem evidência; cite o material público ou marque pending/uncertain.",
    "turno_inadmissivel": "O item {requirement} cita um turno inexistente; use apenas turnos de public_evidence_turns ou current_turn.",
    "citacao_inadmissivel": "O item {requirement} tem citação que não aparece literalmente no material indicado. "
                            "Cite um trecho continuo de um unico turno, copiado sem parafrasear, com no maximo 20 palavras "
                            "(pelo menos duas na fala); nao junte falas de turnos diferentes nem inclua o que o estudante nao escreveu.",
    "resolucao_com_pendencia": "gap_resolved=true com o item {requirement} não satisfeito; satisfação parcial não encerra.",
    "codigo_inalterado": "O critério exige alterar código: cite o código alterado numa fala posterior ao diagnóstico ou não declare resolução.",
}

# Política 1.9.0: frase fixa acrescentada à correção das regras de citação (sem conteúdo recusado).
QUOTE_COPY_RULES = frozenset({"citacao_inadmissivel", "turno_inadmissivel"})
QUOTE_COPY_FEEDBACK = (
    "Copie as palavras do estudante exatamente como ele as escreveu, do turno indicado, e informe em turn o "
    "numero correto desse turno; copie o codigo como foi escrito. As citacoes sao conferidas literalmente: "
    "para localizar a citacao, espacos, quebras de linha, indentacao e crases nao importam; palavras e simbolos, sim."
)

CORRECTION_FEEDBACK = {
    ("N0-B", "n0b_criterio_codigo_inconsistente"):
        "A proposta violou n0b_criterio_codigo_inconsistente. Produza novamente o objeto completo. "
        "Se o criterio exigir alterar codigo, funcao, implementacao, logica ou condicao, "
        "requires_code_change deve ser true. Um criterio apenas verbal pode usar false. "
        "Mantenha o escopo publico e todas as regras N0; o contrato ainda nao foi congelado.",
    # Histórico (políticas 1.3.0 e 1.4.0): mantido para analisar lotes anteriores ao canal único.
    ("N0-B", "n0b_requisito_codigo_sem_canal"):
        "A proposta violou n0b_requisito_codigo_sem_canal. O estudante nao apresentou codigo na entrada "
        "diagnostica (student_code_present=false). Produza novamente o objeto completo com criterio e "
        "requisitos demonstraveis por explicacao: evidence_source student_message ou student_message_or_code, "
        "requires_code_change=false. No criterio, nao combine verbos de edicao (corrigir, alterar, ajustar, revisar, "
        "modificar) com codigo, programa, funcao, implementacao, logica, condicao ou condicional; descreva o que o "
        "estudante deve explicar. Mantenha o escopo publico e as demais regras N0.",
    ("N0-B", "n0b_requisito_nao_demonstravel"):
        "A proposta violou n0b_requisito_nao_demonstravel. Algum requisito descreve uma restricao ou proibicao "
        "(o que o estudante nao deve usar ou mencionar). Restricoes do enunciado pertencem ao escopo N0-A e nao "
        "viram requisito. Produza novamente o objeto completo com requisitos que sejam acoes positivas e observaveis "
        "do estudante, demonstraveis por trecho literal da fala (o codigo, quando houver, faz parte da fala). "
        "Mantenha as demais regras N0.",
    ("N0-B", "n0b_claim_sem_ancoragem"):
        "A proposta violou n0b_claim_sem_ancoragem. claim_quote deve ser um trecho literal da fala do estudante, "
        "com pelo menos duas palavras. Produza novamente o objeto completo, copiando o trecho sem parafrasear. "
        "Mantenha as demais regras N0.",
    ("N1-V", "n1v_criterios_incoerentes"):
        "Criterios SELA incoerentes: em PROBING, approved=true exige lacuna_real, nao_revela, ancorada e "
        "categorizavel iguais a atende e socratic_category de Paul e Elder; em encerramentos use nao_se_aplica e nenhuma.",
    ("N1-V", "n1v_resolution_check_incoerente"):
        "Em PROBING ou CLOSING_LIMIT, resolution_check deve ser not_applicable; "
        "em CLOSING_RESOLVED, informe supported, unsupported ou uncertain.",
    ("N2", "n2_contrato_requisitos_inconsistente"):
        "requirement_assessments deve conter exatamente um item para cada requisito do contrato N0 "
        "(mesmos requirement_id, sem repetir), inclusive pendentes ou incertos.",
    ("N2", "n2_redigiu_pergunta"):
        "A proposta violou n2_redigiu_pergunta. Produza novamente o objeto completo. "
        "plan deve descrever somente uma estrategia pedagogica abstrata, sem redigir ou citar "
        "pergunta e sem ponto de interrogacao. N1 redige a pergunta. Preserve o contrato N0 "
        "e interprete a mesma resposta a partir do mesmo estado anterior.",
    ("N2", "n2_resolucao_incompleta"):
        "A proposta violou n2_resolucao_incompleta. Reavalie os mesmos requisitos N0 com as mesmas "
        "entradas públicas. Registre itens pendentes ou incertos. gap_resolved=true exige todos "
        "satisfeitos com evidência admissível; satisfação parcial não encerra. Não redija pergunta "
        "nem altere N0.",
}

REQUIRED_TEXT_FEEDBACK = ("O campo {field} e obrigatorio e nao pode ficar vazio; devolva-o preenchido conforme "
                          "o schema, sem alterar os demais campos.")
CORRECTION_FEEDBACK.update({
    (agent, f"schema_{field}_invalido"): REQUIRED_TEXT_FEEDBACK.format(field=field)
    for agent, field in REQUIRED_TEXT_FIELDS
})


# Lembrete fixo anexado a toda correção do N0-B: corrigir uma regra não pode violar as demais.
N0B_STRUCTURAL_REMINDER = (
    "Lembrete de todas as regras N0: de 1 a 3 requisitos; o codigo, quando houver, vem dentro da fala; se a fala "
    "nao contem codigo, requires_code_change=false e o criterio nao combina verbos de edicao (corrigir, alterar, "
    "ajustar, revisar, modificar) com codigo, funcao, logica, condicao ou implementacao; requisitos sao acoes "
    "positivas e observaveis do estudante, sem restricoes ou proibicoes."
)


def correction_feedback(agent, error_kind, rule=None, requirement_id=None, required_source=None):
    """Feedback fixo da regra; a política 1.2.0 acrescenta a frase pré-definida da violação."""
    text = CORRECTION_FEEDBACK[(agent, error_kind)]
    if TUTOR_CORRECTION_POLICY.get("rule_specific_feedback") and rule in GUARD_RULE_FEEDBACK:
        detail = GUARD_RULE_FEEDBACK[rule].format(requirement=requirement_id or "indicado", source=required_source or "exigida")
        text += f" Regra violada: {rule}. {detail}"
        if TUTOR_CORRECTION_POLICY.get("quote_copy_feedback") and rule in QUOTE_COPY_RULES:
            text += " " + QUOTE_COPY_FEEDBACK
    if agent == "N0-B":
        text += " " + N0B_STRUCTURAL_REMINDER
    return text


def correction_rule_allowed(agent, error_kind, version):
    """A correction is valid only under the policy version recorded with it."""
    policy = TUTOR_CORRECTION_POLICIES.get(version)
    return policy is not None and error_kind in policy["retryable_rules"] and (agent, error_kind) in CORRECTION_FEEDBACK
