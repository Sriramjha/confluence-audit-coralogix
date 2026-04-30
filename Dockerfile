# Ship Atlassian Cloud audit logs to Coralogix (POST .../logs/v1/singles — not legacy /api/v1/logs).
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt main.py ./

RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --no-create-home --uid 65532 collector

USER collector

ENV PYTHONUNBUFFERED=1

# Defaults only when unset; pass real secrets with docker run -e / --env-file.
ENV ATLASSIAN_AUDIT_PRODUCT=confluence

ENTRYPOINT ["python", "main.py"]
