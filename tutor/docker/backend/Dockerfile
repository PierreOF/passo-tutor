FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY src/ src/
COPY tests/ tests/
COPY avaliacao/exercicios/ avaliacao/exercicios/
COPY avaliacao/perfis/ avaliacao/perfis/
COPY avaliacao/roteiros/ avaliacao/roteiros/
RUN pip install --no-cache-dir -e '.[dev]' && useradd --uid 10001 --create-home passo
COPY docker/backend/entrypoint.py docker/backend/entrypoint.py
USER passo
ENTRYPOINT ["python", "/app/docker/backend/entrypoint.py"]
CMD ["python", "-m", "tutor_socratico.api"]
