# Atlassian Cloud audit logs → Coralogix

Collect administrator audit records from **Confluence Cloud** or **Jira Cloud** and send each event to **[Coralogix](https://coralogix.com/)** using the current ingestion API:

`POST https://ingress.<CORALOGIX_DOMAIN>/logs/v1/singles`  
Header: `Authorization: Bearer <Send-Your-Data API key>`

This project replaces workflows based on **`coralogixrepo/coralogix-audit-collector`**, which documented **`CORALOGIX_LOG_URL=…/api/v1/logs`**. That URL pattern is part of Coralogix’s legacy ingestion deprecation; use **`CORALOGIX_DOMAIN`** and **`/logs/v1/singles`** instead (see [Coralogix ingestion deprecation notices](https://coralogix.com/docs/user-guides/latest-updates/deprecations/endpoints/) and [REST API singles](https://coralogix.com/docs/developer-portal/apis/log-ingestion/coralogix-rest-api-singles/)).

**Author:** [@Sriramjha](https://github.com/Sriramjha)

---

## What you need

- **Python 3.10+**
- Atlassian Cloud site: `https://<org>.atlassian.net`
- Atlassian account **email** + **API token** from [Atlassian API tokens](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-account/)
- Correct **product permission** for the mode you choose (see below)
- Coralogix **Send-Your-Data** API key and your **team domain** (for example `ap3.coralogix.com`, `eu2.coralogix.com` — see [Coralogix domains](https://coralogix.com/docs/user-guides/account-management/account-settings/coralogix-domain/))

### Confluence mode (`ATLASSIAN_AUDIT_PRODUCT=confluence`)

- Confluence **product** licensed on that site
- User must be a **Confluence administrator** (audit API)
- API: `GET …/wiki/rest/api/audit`

### Jira mode (`ATLASSIAN_AUDIT_PRODUCT=jira`)

- Use when the site has **Jira** but **no Confluence** (Confluence audit often returns `403` “not permitted to use Confluence”)
- User needs global permission **Administer Jira**
- API: `GET …/rest/api/3/auditing/record`

---

## Step 1 — Clone the repository

```bash
git clone https://github.com/Sriramjha/confluence-audit-coralogix.git
cd confluence-audit-coralogix
```

---

## Step 2 — Python virtual environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## Step 3 — Configuration (`env.sh`)

Copy the template and lock down permissions:

```bash
cp env.example env.sh
chmod 600 env.sh
```

Edit **`env.sh`** with real values. Minimum:

| Variable | Purpose |
|----------|---------|
| `CONFLUENCE_USERNAME` or `CONFLUENCE_EMAIL` or `ATLASSIAN_EMAIL` | Atlassian account email for HTTP Basic auth |
| `CONFLUENCE_API_TOKEN` or `ATLASSIAN_API_TOKEN` | API token for that account |
| `BASE_URL` | Full site URL, e.g. `https://your-org.atlassian.net` (or set `CONFLUENCE_SITE` to the hostname only) |
| `ATLASSIAN_AUDIT_PRODUCT` | `confluence` (default) or `jira` |
| `CORALOGIX_PRIVATE_KEY` | Coralogix Send-Your-Data key |
| `CORALOGIX_DOMAIN` | Region host **without** `https://`, e.g. `ap3.coralogix.com` |

**Optional (common):**

| Variable | Purpose |
|----------|---------|
| `INTEGRATION_SEARCH_DIFF_IN_MINUTES` | Rolling window in minutes (recommended with frequent cron); when set, overrides plain calendar defaults unless you also set explicit start/end dates |
| `CORALOGIX_APP_NAME` / `CX_APPLICATION_NAME` | Coralogix `applicationName` |
| `INTEGRATION_NAME` / `CX_SUBSYSTEM_NAME` | Coralogix `subsystemName` |
| `CONFLUENCE_START_DATE` / `CONFLUENCE_END_DATE` | Fixed audit window (calendar or ISO); see tool behaviour in `main.py` |
| `CONFLUENCE_CLOUD_ID`, `JIRA_CLOUD_ID`, `ATLASSIAN_CLOUD_ID` | Use Atlassian gateway URLs when you do not call the site hostname directly |
| `CORALOGIX_LOG_URL` | If `CORALOGIX_DOMAIN` is empty, region can be inferred from an ingress URL hostname |
| `DEBUG=true` | Prints resolved settings (secrets redacted) |
| `CONFLUENCE_SEARCH`, `JIRA_AUDIT_FILTER` | Optional server-side filter (same role as `--search` where applicable) |
| `CONFLUENCE_PAGE_LIMIT`, `CORALOGIX_BATCH_SIZE`, `CONFLUENCE_MIN_INTERVAL_SEC` | Tuning pagination and ingest |

Full list and edge cases: docstring at the top of **`main.py`**.

**Important:** Every time you run `source env.sh`, variables in **`env.sh`** win over whatever you typed earlier in the shell. Set **`ATLASSIAN_AUDIT_PRODUCT=jira`** inside **`env.sh`** if you need Jira mode permanently.

---

## Step 4 — Verify connectivity (`--diagnose`)

```bash
source env.sh
.venv/bin/python main.py --diagnose
```

This lists sites visible to the token (`accessible-resources`) and probes **Confluence** `…/user/current` or **Jira** `…/myself`. Fix **401** (wrong email/token) or **403** (missing product or admin permission) before going live.

---

## Step 5 — Dry run (Atlassian only, no Coralogix)

```bash
source env.sh
.venv/bin/python main.py --dry-run
```

Confirms audit fetch and prints how many records were read; does **not** POST to Coralogix.

---

## Step 6 — Production run

```bash
source env.sh
.venv/bin/python main.py
```

Sends batches to Coralogix **`/logs/v1/singles`**.

### CLI overrides

```text
--start-date / --end-date   Audit window (overrides env calendar defaults when used with explicit dates)
--search                    Confluence `searchString` or Jira `filter` query
--dry-run                   Fetch only
--diagnose                  Connectivity probe then exit
```

---

## Step 7 — Cron without Docker

Create **`run.sh`**:

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

Crontab example (every 5 minutes):

```cron
*/5 * * * * /path/to/confluence-audit-coralogix/run.sh >> /var/log/confluence-audit-cx.log 2>&1
```

---

## Docker

### Build

```bash
cd /path/to/confluence-audit-coralogix
docker build -t confluence-audit-coralogix:local .
```

### Run with inline environment (same idea as the old collector)

Use **`CORALOGIX_DOMAIN`**, not **`CORALOGIX_LOG_URL=…/api/v1/logs`**:

```bash
docker run --rm \
  -e CORALOGIX_DOMAIN="ap3.coralogix.com" \
  -e CORALOGIX_PRIVATE_KEY="YOUR_SEND_YOUR_DATA_KEY" \
  -e CORALOGIX_APP_NAME="atlassian" \
  -e CONFLUENCE_USERNAME="you@company.com" \
  -e CONFLUENCE_API_TOKEN="YOUR_ATLASSIAN_API_TOKEN" \
  -e INTEGRATION_NAME="confluence" \
  -e BASE_URL="https://your-org.atlassian.net" \
  -e ATLASSIAN_AUDIT_PRODUCT="confluence" \
  -e DEBUG="true" \
  -e INTEGRATION_SEARCH_DIFF_IN_MINUTES="5" \
  confluence-audit-coralogix:local
```

For **Jira-only** tenants, set **`ATLASSIAN_AUDIT_PRODUCT=jira`**.

### Run with a secrets file (recommended)

```bash
chmod 600 /etc/confluence-audit-coralogix.env
docker run --rm --env-file /etc/confluence-audit-coralogix.env confluence-audit-coralogix:local
```

### Cron with Docker

```cron
*/5 * * * * docker run --rm --env-file /etc/confluence-audit-coralogix.env confluence-audit-coralogix:local >> /var/log/confluence-audit-cx.log 2>&1
```

Prefer **`docker run --rm`** instead of removing a named container every tick unless you rely on a fixed container name.

---

## Migrating from `coralogixrepo/coralogix-audit-collector`

| Old environment | Use instead |
|-----------------|-------------|
| `CORALOGIX_LOG_URL=https://ingress.<region>.coralogix.com/api/v1/logs` | `CORALOGIX_DOMAIN=<region>.coralogix.com` (this tool builds `/logs/v1/singles` itself) |
| `CORALOGIX_PRIVATE_KEY` | Same |
| `CORALOGIX_APP_NAME` | Same |
| `CONFLUENCE_USERNAME`, `CONFLUENCE_API_TOKEN` | Same (`ATLASSIAN_*` aliases supported) |
| `INTEGRATION_NAME` | Same |
| `BASE_URL` | Same |
| `DEBUG`, `INTEGRATION_SEARCH_DIFF_IN_MINUTES` | Same |
| (implicit Confluence integration) | Add **`ATLASSIAN_AUDIT_PRODUCT=jira`** if you never had Confluence |

The upstream collector image is **Go**-based; this repo ships a **Python** collector with an equivalent operational shape but the supported Coralogix endpoint is **`/logs/v1/singles`**.

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| `403` “not permitted to use Confluence” | Jira-only site: set **`ATLASSIAN_AUDIT_PRODUCT=jira`**. Otherwise grant Confluence product + Confluence admin. |
| `403` on Jira audit | **Administer Jira** and correct API scopes for granular tokens |
| `401` on diagnose `accessible-resources` | Email must match the token owner; regenerate token |
| Coralogix errors | Region (`CORALOGIX_DOMAIN`), key type (Send-Your-Data), network egress |

---

## References

- [Confluence audit API](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-audit/#api-wiki-rest-api-audit-get)
- [Jira audit records API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-audit-records/#api-rest-api-3-auditing-record-get)
- [Coralogix REST singles](https://coralogix.com/docs/developer-portal/apis/log-ingestion/coralogix-rest-api-singles/)

---

## License

Use and modify for your organization. No warranty implied.
