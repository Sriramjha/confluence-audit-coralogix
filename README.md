# Confluence audit logs → Coralogix

Ship **Confluence Cloud** administrator audit records to **[Coralogix](https://coralogix.com/)** over HTTPS (`/logs/v1/singles`).

**Author:** [@Sriramjha](https://github.com/Sriramjha)

## What you need

- Confluence **Cloud** with audit logging, and a user with **Confluence admin**
- Atlassian **API token** for that user
- Coralogix **Send-Your-Data** API key and your **region** (e.g. `ap3.coralogix.com`)
- **Python 3.10+**

## Deploy in 5 steps

### 1. Clone this repo

```bash
git clone https://github.com/Sriramjha/confluence-audit-coralogix.git
cd confluence-audit-coralogix
```

### 2. Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Environment variables (one file)

```bash
cp env.example env.sh
# Edit env.sh with real secrets, then:
chmod 600 env.sh
```

Or export the same variables in your shell (see `env.example`).

### 4. Run

```bash
source env.sh
.venv/bin/python main.py --dry-run   # Confluence only, no Coralogix
.venv/bin/python main.py             # production
```

### 5. Cron (optional)

Create `run.sh`:

```bash
#!/bin/sh
set -e
cd /path/to/confluence-audit-coralogix
. ./env.sh
exec .venv/bin/python main.py
```

```bash
chmod 700 run.sh
```

Crontab (every 5 minutes):

```cron
*/5 * * * * /path/to/confluence-audit-coralogix/run.sh >> /var/log/confluence-audit-cx.log 2>&1
```

## Configuration highlights

| Variable | Purpose |
|----------|---------|
| `CONFLUENCE_USERNAME` or `CONFLUENCE_EMAIL` | Basic auth user (email) |
| `CONFLUENCE_API_TOKEN` | Atlassian API token |
| `BASE_URL` or `CONFLUENCE_SITE` | Site, e.g. `https://company.atlassian.net` |
| `CORALOGIX_PRIVATE_KEY` | Send-Your-Data key |
| `CORALOGIX_DOMAIN` | Region host, e.g. `ap3.coralogix.com` |
| `INTEGRATION_SEARCH_DIFF_IN_MINUTES` | Rolling window (good with frequent cron) |

Full list: see the docstring at the top of `main.py`.

## References

- [Confluence Audit API](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-audit/#api-wiki-rest-api-audit-get)
- [Coralogix REST singles](https://coralogix.com/docs/developer-portal/apis/log-ingestion/coralogix-rest-api-singles/)

## License

Use and modify for your organization. No warranty implied.
