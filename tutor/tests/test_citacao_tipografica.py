"""Tolerância tipográfica das citações literais (ADR 0019, decisão do autor de 22/10/2026).

Fixtures: falas reais do piloto `piloto_final_20261022_v9` (E03-P04 e E02-P05), transcritas da
investigação `citacao_inadmissivel` de 22/10/2026. A citação continua literal: mesmas palavras e
símbolos, na mesma ordem, de um só turno e só da fala do estudante. Só a tipografia deixa de contar.
"""

from __future__ import annotations

import unicodedata
import unittest
from typing import Any, Mapping

from instrumento_avaliacao.auditoria_eventos import accepted_agent_events
from tutor_socratico.contratos.esquemas import SchemaValidationError, validate_payload
from tutor_socratico.contratos.modelos import (
    DiagnosisContract,
    PublicEvidenceTurn,
    ResolutionRequirement,
)
from tutor_socratico.contratos.politica_correcao import (
    QUOTE_COPY_FEEDBACK,
    TUTOR_CORRECTION_POLICIES,
    TUTOR_CORRECTION_POLICY,
    correction_feedback,
    correction_rule_allowed,
)
from tutor_socratico.infraestrutura.llm import AdapterError, LLMRequest, LLMResult
from tutor_socratico.sela.agentes import AUDIT_TEXT_LIMIT, AgentRuntime
from tutor_socratico.sela.citacao import locate_quote, quote_admissible
from tutor_socratico.sela.erros import ResolutionGuardError
from tutor_socratico.sela.nivel2.resolucao import (
    changed_after_diagnosis,
    validate_resolution_assessment,
)

# --- E03-P04 (session c608d20d): T1 diagnóstico, T2, T3 pendente rejeitado no v9 -------------------
E03_T1 = (
    "Estou fazendo este exercício:\n\nEm Python, escreva uma função maior_valor(valores) que retorne o maior "
    "número de uma lista não vazia sem utilizar a função max.\n\nOi, tô com um problema nesse exercício aqui. "
    "A função até funciona em alguns casos, mas quando eu testo com uma lista só de números negativos, tipo "
    "[-5, -2, -10], ela me devolve 0 em vez do -2, que é o maior. Acho que o erro tá em algum lugar, mas não tô "
    "conseguindo ver. Pode me ajudar a achar onde tá a falha?\n\n```python\ndef maior_valor(valores):\n"
    "    maior = 0\n    for valor in valores:\n        if valor > maior:\n            maior = valor\n"
    "    return maior\n```"
)
E03_T2 = (
    "Ah, acho que tô começando a ver o problema. No começo, o `maior` vale 0, né? Aí quando entra o -5, a "
    "condição `-5 > 0` é falsa, então não atualiza. Depois com o -2, mesma coisa, `-2 > 0` é falso. E o -10 "
    "também. No fim, retorna 0, que nunca foi substituído.\n\nEntão o problema é essa inicialização com 0, "
    "certo? Mas não sei bem como resolver sem perder a lógica. Em vez de começar com 0, eu poderia começar com "
    "o primeiro elemento da lista? Tipo, pegar o `valores[0]` antes do loop?\n\nAh, mas aí eu não posso "
    "modificar o código ainda, né? Você consegue me dizer qual linha tá errada pra eu ter certeza antes de mudar?"
)
E03_TUTOR_T2 = "Sua intuição está correta — maior = 0 é a linha problemática. Como ficaria essa inicialização usando valores[0]?"
E03_T3 = (
    "Ah, então era essa linha mesmo! Agora que você confirmou, faz sentido — o 0 nunca vai ser substituído se "
    "todos os valores forem menores que ele. \n\nPra inicializar com o primeiro elemento, eu tiraria o "
    "`maior = 0` e colocaria algo como `maior = valores[0]` antes do loop. Aí o `for` continuaria igual, "
    "percorrendo do segundo elemento em diante (ou de todo mundo, tanto faz, porque na primeira iteração o valor "
    "vai ser igual ao maior e a condição `valor > maior` vai ser falsa). \n\nFicaria mais ou menos assim:\n\n"
    "python\ndef maior_valor(valores):\n    maior = valores[0]\n    for valor in valores:\n"
    "        if valor > maior:\n            maior = valor\n    return maior\n\n\nTá certo assim? Ou tem algum "
    "detalhe que eu tô deixando passar, tipo e se a lista vier vazia? Mas o enunciado fala que é não vazia, "
    "então acho que não precisa se preocupar com isso, né?"
)
E03_BLOCK = "def maior_valor(valores):\n    maior = valores[0]\n    for valor in valores:"

# --- E02-P05 (session 07cb3fb3): T2, T5 e T6 pendente rejeitado no v9 -------------------------------
E02_T2 = (
    "Hmm, se eu olhar uma nota só... acho que eu teria que ver se ela é maior que zero, né? Mas aí eu não sei "
    "como fazer pra olhar uma nota de cada vez, se é com um for ou sei lá. Pode me mostrar um exemplo pequeno de "
    "como começar? Tipo só essa parte de ver nota por nota, depois eu tento completar."
)
E02_T5 = (
    "Ah, com soma e contagem eu sei que é pra dividir um pelo outro, tipo 15 dividido por 3 dá 5. Mas o problema "
    "é que eu não sei nem como começar a escrever o for no Python pra olhar nota por nota. Tipo, eu não sei se é "
    "for nota in notas: ou se é outro jeito. Pode me dar só essa primeira parte escrita pra eu copiar? Depois eu "
    "tento juntar com a soma e a divisão."
)
E02_T6 = (
    "Hmm, se a lista for [0, -2, -5], todas as notas são zero ou negativas, então não tem nenhuma nota válida "
    "maior que zero. O enunciado diz pra retornar None nesse caso. Mas eu ainda tô travado em como escrever o "
    "começo, tipo o for pra olhar nota por nota. Se eu fizer assim funciona?\n\npython\n"
    "def media_notas_positivas(notas):\n    for nota in notas:\n        # aqui eu não sei o que fazer\n\n\n"
    "Tá certo esse começo? A minha dúvida agora é: depois desse for, onde eu coloco a parte de somar e contar? "
    "Dentro do for ou fora? E como eu faço pra guardar a soma e a contagem se elas começam valendo zero?"
)
# Único caso real registrado em texto (v5, contract_gap): o N2 omitiu as aspas internas.
V5_SPEECH = 'Eu não sei como fazer esse "pegar só alguns" no Python, só sei somar tudo.'


class TypographicVariantsAreAccepted(unittest.TestCase):
    def test_each_typographic_variant_is_accepted(self) -> None:
        cases = {
            "crases omitidas": ("colocaria algo como maior = valores[0] antes do loop", E03_T3),
            "crases mantidas": ("colocaria algo como `maior = valores[0]` antes do loop", E03_T3),
            "crases acrescentadas": ("eu não sei se é `for nota in notas:` ou se é outro jeito", E02_T5),
            "cerca restaurada": ("```python\n" + E03_BLOCK, E03_T3),
            "bloco exato": (E03_BLOCK, E03_T3),
            "reindentado com 2 espaços": (E03_BLOCK.replace("    ", "  "), E03_T3),
            "sem indentação": (E03_BLOCK.replace("    ", ""), E03_T3),
            "tabulação": (E03_BLOCK.replace("    ", "\t"), E03_T3),
            "linhas unidas": ("def maior_valor(valores): maior = valores[0] for valor in valores:", E03_T3),
            "CRLF": (E03_BLOCK.replace("\n", "\r\n"), E03_T3),
            "bloco E02 em uma linha": ("def media_notas_positivas(notas): for nota in notas:", E02_T6),
            "comentário com 4 espaços (fala tem 8)": ("for nota in notas:\n    # aqui eu não sei o que fazer", E02_T6),
            "NFD": (unicodedata.normalize("NFD", "não tem nenhuma nota válida maior que zero"), E02_T6),
            "reticência tipográfica": ("se eu olhar uma nota só… acho que", E02_T2),
            "reticência na fala, pontos na citação": ("olhar uma nota só... acho", E02_T2.replace("...", "…")),
            "caixa": ("O PROBLEMA é essa inicialização com 0", E03_T2),
            "aspas tipográficas": ("fazer esse “pegar só alguns” no Python", V5_SPEECH),
            "crase no lugar de aspas": ("fazer esse `pegar só alguns` no Python", V5_SPEECH),
            "aspas no lugar de crases": ('colocaria algo como "maior = valores[0]" antes do loop', E03_T3),
        }
        for name, (quote, speech) in cases.items():
            with self.subTest(name):
                self.assertTrue(quote_admissible(quote, speech))

    def test_located_excerpt_is_the_students_own_text(self) -> None:
        self.assertEqual(
            locate_quote("colocaria algo como maior = valores[0] antes do loop", E03_T3),
            "colocaria algo como `maior = valores[0]` antes do loop",
        )
        self.assertEqual(locate_quote(E03_BLOCK.replace("    ", ""), E03_T3), E03_BLOCK)
        self.assertIsNone(locate_quote("texto que o aluno nunca escreveu", E03_T3))


class NonTypographicDifferencesAreRejected(unittest.TestCase):
    def test_words_symbols_and_order_still_count(self) -> None:
        cases = {
            "paráfrase": ("eu trocaria o maior = 0 por maior = valores[0]", E03_T3),
            "palavra reordenada": ("algo colocaria como maior = valores[0] antes do loop", E03_T3),
            "palavra trocada": ("colocaria algo como maior = valores[0] depois do loop", E03_T3),
            "fala do tutor": (E03_TUTOR_T2, E03_T3),
            "aspas omitidas (v5)": ("não sei como fazer esse pegar só alguns no Python", V5_SPEECH),
            "literal de string": ('eu mudei pra x = 5 e deu erro', 'eu mudei pra x = "5" e deu erro'),
            "espaço removido entre símbolos": ("colocaria algo como maior=valores[0]", E03_T3),
            "acentos removidos": ("nao tem nenhuma nota valida maior que zero", E02_T6),
            "trechos não contíguos": ("o for pra olhar nota por nota, todas as notas são zero ou negativas", E02_T6),
            "elisão": ("eu tiraria o `maior = 0` e colocaria ... `maior = valores[0]`", E03_T3),
            "meio de palavra": ("aior = valores[0] antes", E03_T3),
            "uma palavra só": ("`valores`", E03_T3),
            "vazia": ("``` ```", E03_T3),
        }
        for name, (quote, speech) in cases.items():
            with self.subTest(name):
                self.assertFalse(quote_admissible(quote, speech))

    def test_string_literal_quote_marks_stay_equivalent_not_optional(self) -> None:
        self.assertTrue(quote_admissible("x = '5' e deu", 'eu mudei pra x = "5" e deu erro'))
        self.assertFalse(quote_admissible("x = 5 e deu", 'eu mudei pra x = "5" e deu erro'))
        # Leitura de 15/10/2026, mantida: crase vale aspas, então "5" casa com `5` (registrado na ADR 0019).
        self.assertTrue(quote_admissible('x = "5" e deu', "eu mudei pra x = `5` e deu erro"))

    def test_backtick_between_word_characters_does_not_join_tokens(self) -> None:
        self.assertIsNone(locate_quote("foobar baz", "foo`bar baz"))
        self.assertIsNone(locate_quote("ab cd", "`a``b` cd"))
        self.assertEqual(locate_quote("foo`bar baz", "foo`bar baz"), "foo`bar baz")


def e03_diagnosis() -> DiagnosisContract:
    return DiagnosisContract(
        "não percebe a inicialização", "conceitual", "maior começa em 0",
        "apresenta o código corrigido com maior inicializado por um elemento da lista", True, "4.0.0",
        (ResolutionRequirement("R01", "identifica que maior = 0 é a causa"),
         ResolutionRequirement("R02", "apresenta o código corrigido com maior = valores[0]")),
    )


def e03_payload(r02: dict[str, Any], *, resolved: bool = True) -> dict[str, Any]:
    return {
        "gap_resolved": resolved,
        "resolution_evidence": "R01 e R02 citados.",
        "requirement_assessments": [
            {"requirement_id": "R01", "status": "satisfied", "reason": "identificou",
             "evidence": [{"turn": 2, "quote": "o problema é essa inicialização com 0, certo?"}]},
            {"requirement_id": "R02", "status": "satisfied", "reason": "corrigiu", "evidence": [r02]},
        ],
    }


E03_HISTORY = (PublicEvidenceTurn(1, E03_T1), PublicEvidenceTurn(2, E03_T2))


class GuardWithRealV9Turns(unittest.TestCase):
    def validate(self, r02: dict[str, Any], current: PublicEvidenceTurn | None = None) -> None:
        validate_resolution_assessment(
            e03_diagnosis(), e03_payload(r02), E03_HISTORY, current or PublicEvidenceTurn(3, E03_T3), E03_T1,
        )

    def test_quote_without_backticks_resolves_e03_p04(self) -> None:
        self.assertIsNone(self.validate({"turn": 3, "quote": "colocaria algo como maior = valores[0] antes do loop"}))
        self.assertIsNone(self.validate({"turn": 3, "quote": "def maior_valor(valores): maior = valores[0]"}))

    def rejected(self, r02: dict[str, Any]) -> ResolutionGuardError:
        with self.assertRaises(ResolutionGuardError) as caught:
            self.validate(r02)
        return caught.exception

    def test_wrong_turn_is_still_rejected_and_audited(self) -> None:
        error = self.rejected({"turn": 2, "quote": "maior = valores[0]"})
        self.assertEqual((error.guard_rule, error.guard_requirement_id), ("citacao_inadmissivel", "R02"))
        self.assertEqual((error.guard_turn, error.guard_quote), (2, "maior = valores[0]"))

    def test_tutor_speech_is_still_rejected(self) -> None:
        error = self.rejected({"turn": 3, "quote": "Como ficaria essa inicialização usando valores[0]?"})
        self.assertEqual(error.guard_rule, "citacao_inadmissivel")

    def test_unknown_turn_records_quote_and_turn(self) -> None:
        error = self.rejected({"turn": 9, "quote": "maior = valores[0]"})
        self.assertEqual((error.guard_rule, error.guard_turn, error.guard_quote), ("turno_inadmissivel", 9, "maior = valores[0]"))


# Defeito do diagnóstico: o return está dentro do for (8 espaços). A correção só reindenta o return.
INDENT_DIAGNOSIS = (
    "Minha função devolve cedo:\n\ndef maior_valor(valores):\n    maior = valores[0]\n"
    "    for valor in valores:\n        if valor > maior:\n            maior = valor\n        return maior\n"
)
INDENT_FIX = INDENT_DIAGNOSIS.replace("Minha função devolve cedo:", "Tirei o return de dentro do for:").replace(
    "        return maior", "    return maior")
COLLAPSED_LOOP = "for valor in valores: if valor > maior: maior = valor return maior"


class CodeChangedKeepsStrictComparison(unittest.TestCase):
    """A citação localiza o trecho na fala; a mudança de código compara esse trecho, estrito, com o diagnóstico."""

    @staticmethod
    def assessments(quote: str, turn: int = 2) -> list[Mapping[str, Any]]:
        return [{"requirement_id": "R01", "status": "satisfied", "evidence": [{"turn": turn, "quote": quote}]}]

    def test_reindent_only_fix_counts_as_changed_even_if_quoted_collapsed(self) -> None:
        # Pela régua tolerante, a citação colapsada também "aparece" no diagnóstico: por isso ela não decide.
        self.assertTrue(quote_admissible(COLLAPSED_LOOP, INDENT_DIAGNOSIS))
        self.assertTrue(changed_after_diagnosis(self.assessments(COLLAPSED_LOOP), INDENT_DIAGNOSIS, {2: INDENT_FIX}))

    def test_unchanged_code_quoted_collapsed_counts_as_unchanged(self) -> None:
        repeated = INDENT_DIAGNOSIS.replace("Minha função devolve cedo:", "Mandei de novo:")
        self.assertFalse(changed_after_diagnosis(self.assessments(COLLAPSED_LOOP), INDENT_DIAGNOSIS, {2: repeated}))

    def test_guard_accepts_reindent_fix_and_rejects_repeated_code(self) -> None:
        diagnosis = DiagnosisContract(
            "return dentro do for", "conceitual", "a função devolve cedo", "corrige o código", True, "4.0.0",
            (ResolutionRequirement("R01", "apresenta o código corrigido"),),
        )
        payload = {"gap_resolved": True, "resolution_evidence": "R01",
                   "requirement_assessments": [dict(self.assessments(COLLAPSED_LOOP)[0], reason="corrigiu")]}
        history = (PublicEvidenceTurn(1, INDENT_DIAGNOSIS),)
        self.assertIsNone(validate_resolution_assessment(
            diagnosis, payload, history, PublicEvidenceTurn(2, INDENT_FIX), INDENT_DIAGNOSIS))
        with self.assertRaises(ResolutionGuardError) as caught:
            validate_resolution_assessment(diagnosis, payload, history, PublicEvidenceTurn(2, INDENT_DIAGNOSIS), INDENT_DIAGNOSIS)
        self.assertEqual(caught.exception.guard_rule, "codigo_inalterado")

    def test_formatting_backticks_do_not_count_as_code_change(self) -> None:
        diagnosis = "if x > 0:\n    total = x"
        later = "Mandei de novo:\n`if x > 0:`\n`    total = x`"
        quote = "if x > 0: total = x"
        self.assertTrue(quote_admissible(quote, later))
        self.assertFalse(changed_after_diagnosis(self.assessments(quote), diagnosis, {2: later}))
        # Indentação continua contando mesmo com crases.
        self.assertTrue(changed_after_diagnosis(self.assessments(quote), "if x > 0:\n        total = x", {2: later}))

    def test_without_turn_material_the_quote_itself_is_compared_strictly(self) -> None:
        # Chamada antiga (concordância): sem falas, a citação é comparada como está, sem colapsar espaços.
        self.assertTrue(changed_after_diagnosis(self.assessments(COLLAPSED_LOOP), INDENT_DIAGNOSIS))
        self.assertFalse(changed_after_diagnosis(self.assessments("maior = valores[0]"), INDENT_DIAGNOSIS))


class ScriptedAdapter:
    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    def generate(self, request: LLMRequest) -> LLMResult:
        self.prompts.append(request.messages[0].content)
        return LLMResult(self.payloads.pop(0), "m", "m", "local", generation_id=f"gen-{len(self.prompts)}")


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["plan", "items"],
    "properties": {"plan": {"type": "string", "minLength": 1},
                   "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                        "required": ["status"],
                                                        "properties": {"status": {"type": "string", "enum": ["ok"]}}}}},
}


class AuditOfRejections(unittest.TestCase):
    def test_guard_rejection_records_bounded_quote_turn_and_requirement(self) -> None:
        long_quote = "palavra " * 200
        def guard(payload: Mapping[str, Any]) -> None:
            if payload["plan"] == "primeira":
                raise ResolutionGuardError("n2_resolucao_incompleta", "citacao_inadmissivel", "R02", turn=3, quote=long_quote)
        runtime = AgentRuntime(ScriptedAdapter({"plan": "primeira", "items": []}, {"plan": "segunda", "items": []}))
        runtime.call("N2", "c1", 3, "s", SCHEMA, "prompt", {}, guard=guard)
        rejected, accepted = runtime.execution_events
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual((rejected["guard_rule"], rejected["guard_requirement_id"], rejected["guard_turn"]),
                         ("citacao_inadmissivel", "R02", 3))
        self.assertEqual(rejected["guard_quote"], long_quote[:AUDIT_TEXT_LIMIT])
        self.assertLessEqual(AUDIT_TEXT_LIMIT, 500)
        self.assertNotIn("guard_quote", accepted)
        # Os campos novos são opcionais para o instrumento: o par de correção continua válido.
        events = [dict(e, correction_policy_version=TUTOR_CORRECTION_POLICY["version"]) for e in runtime.execution_events]
        self.assertTrue(accepted_agent_events(events)[1])

    def test_schema_failure_records_field_path_and_message(self) -> None:
        for payload, fragment in (
            ({"plan": "x"}, "items"),
            ({"plan": "x", "items": [], "extra": 1}, "extra"),
            ({"plan": "x", "items": [{"status": "talvez"}]}, "items[0].status"),
        ):
            with self.subTest(fragment=fragment):
                runtime = AgentRuntime(ScriptedAdapter(payload))
                with self.assertRaises(AdapterError) as caught:
                    runtime.call("N2", "c1", 1, "s", SCHEMA, "prompt", {})
                self.assertEqual(caught.exception.kind, "schema_invalido")
                event = runtime.execution_events[-1]
                self.assertIn(fragment, event["schema_error"])
                self.assertLessEqual(len(event["schema_error"]), AUDIT_TEXT_LIMIT)

    def test_schema_messages_name_missing_and_unexpected_fields(self) -> None:
        with self.assertRaisesRegex(SchemaValidationError, "items"):
            validate_payload(SCHEMA, {"plan": "x"})
        with self.assertRaisesRegex(SchemaValidationError, "extra"):
            validate_payload(SCHEMA, {"plan": "x", "items": [], "extra": 1})


class CorrectionPolicy190(unittest.TestCase):
    def test_policy_190_is_current_and_keeps_180_rules(self) -> None:
        self.assertEqual(TUTOR_CORRECTION_POLICY["version"], "1.9.0")
        new, old = TUTOR_CORRECTION_POLICIES["1.9.0"], TUTOR_CORRECTION_POLICIES["1.8.0"]
        self.assertEqual(new["retryable_rules"], old["retryable_rules"])
        self.assertTrue(new["quote_copy_feedback"])
        self.assertNotIn("quote_copy_feedback", old)
        self.assertTrue(correction_rule_allowed("N2", "n2_resolucao_incompleta", "1.9.0"))
        self.assertTrue(correction_rule_allowed("N2", "n2_resolucao_incompleta", "1.8.0"))

    def test_quote_rejections_ask_for_exact_copy_and_correct_turn(self) -> None:
        for fragment in ("exatamente como", "turno indicado", "numero correto", "crases", "palavras e simbolos",
                         "para localizar a citacao", "codigo", "como foi escrito"):
            self.assertIn(fragment, QUOTE_COPY_FEEDBACK)
        for rule in ("citacao_inadmissivel", "turno_inadmissivel"):
            with self.subTest(rule=rule):
                self.assertIn(QUOTE_COPY_FEEDBACK, correction_feedback("N2", "n2_resolucao_incompleta", rule, "R02"))
        self.assertNotIn(QUOTE_COPY_FEEDBACK, correction_feedback("N2", "n2_resolucao_incompleta", "resolucao_com_pendencia", "R02"))

    def test_quote_rejection_reaches_n2_correction_prompt(self) -> None:
        def guard(payload: Mapping[str, Any]) -> None:
            if payload["plan"] == "primeira":
                raise ResolutionGuardError("n2_resolucao_incompleta", "citacao_inadmissivel", "R02", turn=3, quote="x y")
        adapter = ScriptedAdapter({"plan": "primeira", "items": []}, {"plan": "segunda", "items": []})
        AgentRuntime(adapter).call("N2", "c1", 3, "s", SCHEMA, "prompt", {}, guard=guard)
        self.assertNotIn(QUOTE_COPY_FEEDBACK, adapter.prompts[0])
        self.assertIn(QUOTE_COPY_FEEDBACK, adapter.prompts[1])
        self.assertNotIn("x y", adapter.prompts[1].replace(QUOTE_COPY_FEEDBACK, ""))


if __name__ == "__main__":
    unittest.main()
