"""API HTTP do tutor, contas de estudantes e execuções de pesquisa."""

from http.server import ThreadingHTTPServer

from .cli import create_first_admin, create_student, main
from .servicos import (
    CHAT_CONVERSATION,
    CONTRACT_GUARD_KINDS,
    RESEARCH_CONVERSATION,
)
from .servidor import Handler

__all__ = [
    "CHAT_CONVERSATION", "CONTRACT_GUARD_KINDS", "Handler",
    "RESEARCH_CONVERSATION", "ThreadingHTTPServer", "create_first_admin", "create_student", "main",
]
