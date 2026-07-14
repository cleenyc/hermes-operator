FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_OPERATOR_CONFIG=/app/operator.toml

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && addgroup --system operator \
    && adduser --system --ingroup operator --home /app operator \
    && mkdir -p /app/data \
    && chown -R operator:operator /app/data

COPY --chown=operator:operator config/operator.example.toml /app/operator.toml

USER operator

VOLUME ["/app/data"]
EXPOSE 8787

ENTRYPOINT ["hermes-operator"]
CMD ["--config", "/app/operator.toml", "run"]
