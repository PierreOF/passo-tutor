.PHONY: test serve

# Pasta lida pelo painel de revisão de experimentos; o tutor não tem padrão embutido.
export TUTOR_REVIEW_ROOT ?= $(abspath $(CURDIR)/../pesquisa/11_avaliacao)

test:
	PYTHONPATH=src python -m unittest discover -s tests -v

serve:
	PYTHONPATH=src python -m tutor_socratico.api
