# Atlassian audit logs → Coralogix

Ships **Confluence** (`ATLASSIAN_AUDIT_PRODUCT=confluence`) or **Jira** (`=jira`) Cloud audit records to Coralogix using **`POST https://ingress.<domain>/logs/v1/singles`** (Bearer key). Do **not** use legacy **`…/api/v1/logs`** (deprecated).

## Files

| File | Purpose |
|------|---------|
| `main.py` | Collector |
| `requirements.txt` | Python deps |
| `env.example` | Variable template (`cp env.example env.sh`) |
| `Dockerfile` | Container image |

Variables and CLI flags are documented in the **`main.py`** docstring.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp env.example env.sh   # edit & chmod 600 env.sh
source env.sh && .venv/bin/python main.py --dry-run
```

## Docker

```bash
docker build -t confluence-audit-coralogix:local .
docker run --rm --env-file /path/to/secrets.env confluence-audit-coralogix:local
```

Use **`CORALOGIX_DOMAIN`** (e.g. `ap3.coralogix.com`), not `CORALOGIX_LOG_URL` pointing at **`/api/v1/logs`**.

---

MIT/no warranty — use at your own risk.
