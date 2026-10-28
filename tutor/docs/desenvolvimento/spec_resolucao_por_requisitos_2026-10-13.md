# Critério de código na resolução por requisitos

Trecho da especificação interna de resolução por requisitos (13/10/2026, revisão de 20/10/2026) que define quando uma citação do estudante conta como código. A guarda do tutor (`sela/nivel2/resolucao.py`) e o verificador do instrumento seguem este texto, e `tests/test_concordancia_regras.py` lê as listas abaixo para conferir que os dois concordam. O restante da especificação é documento interno da pesquisa.

## Revisão — o que conta como código na mudança após o diagnóstico (20/10/2026)

A regra "Código alterado" do contrato 4.0.0 exige uma citação satisfeita com trecho de código que não aparece em `diagnosis_message`, mas não definia o que é código. O tutor contava palavras como `print`, `in` e `and` em qualquer posição, e "agora print resultado" passava por código. O instrumento só aceitava operador ou palavra-chave no início de linha. Decisão autoral: o critério abaixo passa a integrar esta especificação, e a guarda do tutor e o verificador do instrumento seguem este texto.

- **Critério.** Uma citação parece código quando contém pelo menos um operador ou símbolo de código da lista abaixo, em qualquer posição, ou quando alguma das suas linhas começa com uma palavra-chave Python da lista abaixo. O início de linha admite brancos antes da palavra, como a indentação, e a palavra precisa terminar ali: `iffy` não conta como `if`. A comparação diferencia maiúsculas de minúsculas, como o Python. Antes do exame, só CRLF é normalizado para LF.
- **Operadores e símbolos:** `(` `)` `[` `]` `{` `}` `=` `<` `>` `%` `*` `/` `+`
- **Palavras-chave no início de linha:** `and` `assert` `async` `await` `break` `class` `continue` `def` `del` `elif` `else` `except` `finally` `for` `from` `global` `if` `import` `in` `is` `lambda` `nonlocal` `not` `or` `pass` `raise` `return` `try` `while` `with` `yield`
- **Fora do critério.**
  - `as`, `None`, `True` e `False`: `as` é artigo em português, e os três valores aparecem em prosa ("retorno None"). Código que os usa traz também operador ou outra palavra-chave.
  - Palavra-chave no meio da linha: "usei lambda agora" e "se n for par" são menção em prosa.
  - `print` e outros nomes de função sem parênteses: `print` não é palavra-chave. A chamada `print(x)` conta pelos parênteses.
  - Hífen ou travessão entre espaços (`a - b`): em português, marca aposto ou pausa em prosa. Uma subtração em código vem acompanhada de `=`, `return` ou outro elemento da lista.
- **Leitura conservadora.** Na dúvida, o trecho não é tratado como código: a regra não aceita como mudança de código o que pode ser só menção em prosa. "Aparecer" em `diagnosis_message` continua seguindo a regra da citação, em limites de palavra.
- **Versões.** O contrato de resolução continua 4.0.0 e a política de correção, 1.5.0: o esquema, as regras corrigíveis e os códigos de violação (`codigo_inalterado`) não mudam. Como na revisão de 15/10/2026 das aspas equivalentes, o commit do runtime identifica a mudança.

