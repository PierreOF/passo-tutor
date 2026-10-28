"""Build container-only database URLs from the existing Compose credentials."""

import os
import sys
from urllib.parse import quote


def main() -> None:
    user = quote(os.environ["POSTGRES_USER"], safe="")
    password = quote(os.environ["POSTGRES_PASSWORD"], safe="")
    host = os.getenv("TUTOR_DB_HOST", "postgres")
    port = os.getenv("TUTOR_DB_PORT", "5432")
    base = f"postgresql://{user}:{password}@{host}:{port}/"
    os.environ["DATABASE_URL"] = base + "passo"
    os.environ["TEST_DATABASE_URL"] = base + "passo_test"
    if len(sys.argv) < 2:
        raise SystemExit("Comando do backend ausente.")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
