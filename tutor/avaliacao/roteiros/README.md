# Roteiros congeláveis

`matriz_principal.csv` define os 15 IDs, exercícios, cenários e primeiras falas. `turnos.v1.json` contém todas as falas em ordem, com até dez turnos contáveis por caso (limite de dez turnos por sondagem). A primeira fala de cada caso deve coincidir nos dois arquivos; o executor rejeita divergências.

O tutor não conhece o exercício. Na execução, a primeira fala de cada caso recebe antes o enunciado integral do exercício, como faria um estudante que apresenta a tarefa; o código dos casos S04 vai dentro do próprio texto, em bloco Python.

Nos casos S02, a insistência ocorre após a orientação inicial. Os S04 incluem uma explicação corrigida no segundo turno para verificar o encerramento resolvido; os S05 chegam ao décimo turno sem resolução para verificar o encerramento por limite. A qualificação Replay é separada da campanha definitiva.

O nível permitido, o modelo, os prompts e a configuração ficam no manifesto da execução. Os roteiros de calibração do piloto não integram a matriz principal nem este repositório.
