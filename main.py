#!/usr/bin/env python3
"""
Pull **Atlassian Cloud** admin audit records (**Confluence** and/or **Jira**)
and ship each row to Coralogix via ``POST https://ingress.<domain>/logs/v1/singles``.

Product selection (``ATLASSIAN_AUDIT_PRODUCT``):

* **confluence** — ``GET …/wiki/rest/api/audit`` (site or
  ``https://api.atlassian.com/ex/confluence/<cloudId>/…``).
* **jira** — ``GET …/rest/api/3/auditing/record`` (same ``*.atlassian.net`` site,
  or ``https://api.atlassian.com/ex/jira/<cloudId>/…``). Use this when the account
  has **Jira admin** but **no Confluence** license (Confluence REST returns 403
  “not permitted to use Confluence”).

**Authentication:** HTTP **Basic** auth (Atlassian account **email** +
**API token**). Set ``ATLASSIAN_API_TOKEN`` or ``CONFLUENCE_API_TOKEN``.

**Permissions:**

* Confluence path: **Confluence administrator**; scoped ``read:audit-log:confluence`` if using granular tokens.
* Jira path: **Administer Jira** global permission; scoped ``read:audit-log:jira`` / ``manage:jira-configuration`` as per Atlassian docs.

**Local env files:** variables already set in the process environment are never overwritten.
If ``env.sh`` and/or ``.env`` exist in the **current working directory**, they are merged in that order;
``ENV_FILE`` (optional path) is merged last so it wins on duplicate keys.

Python
------
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python main.py

Environment
-----------
Required:
  CONFLUENCE_API_TOKEN      API token (alias: ATLASSIAN_API_TOKEN)
  CONFLUENCE_EMAIL          Atlassian account email for Basic auth (aliases: CONFLUENCE_USERNAME, ATLASSIAN_EMAIL)
  CONFLUENCE_SITE           Hostname only, e.g. mycompany.atlassian.net — or set BASE_URL instead
                            (unless CONFLUENCE_CLOUD_ID / ATLASSIAN_CLOUD_ID / JIRA_CLOUD_ID is set for gateway URLs)
  CORALOGIX_PRIVATE_KEY     Send-Your-Data API key
  CORALOGIX_DOMAIN          e.g. ap3.coralogix.com, eu2.coralogix.com (no https://). Optional if
                            CORALOGIX_LOG_URL is set and the host can be parsed.

Optional:
  ENV_FILE                  Extra env file path merged after ``env.sh`` / ``.env`` (last wins on duplicates)
  ATLASSIAN_AUDIT_PRODUCT   confluence (default) | jira — which Cloud audit API to call
  CONFLUENCE_CLOUD_ID       Gateway: api.atlassian.com/ex/confluence/{id}/... (CONFLUENCE_SITE not needed)
  ATLASSIAN_CLOUD_ID        Same cloud UUID for gateway URLs if you prefer one variable (see JIRA_CLOUD_ID)
  JIRA_CLOUD_ID             Gateway: api.atlassian.com/ex/jira/{id}/... (fallback: ATLASSIAN_CLOUD_ID, CONFLUENCE_CLOUD_ID)
  CONFLUENCE_START_DATE     startDate query (string; often YYYY-MM-DD or full ISO)
  CONFLUENCE_END_DATE       endDate query
  INTEGRATION_SEARCH_DIFF_IN_MINUTES  Rolling window: end=now UTC, start=now−N minutes (ISO strings).
                            Default when unset: calendar CONFLUENCE_*_DATE (~24h by date) or explicit dates.
  CONFLUENCE_LOOKBACK_MINUTES        Alias for the same rolling window if INTEGRATION_SEARCH_DIFF_IN_MINUTES is unset.
  CORALOGIX_LOG_URL         If CORALOGIX_DOMAIN is empty, region is parsed from this URL's hostname
                            (e.g. https://ingress.ap3.coralogix.com/... → ap3.coralogix.com).
  CORALOGIX_APP_NAME        Maps to applicationName; overridden by CX_APPLICATION_NAME
  INTEGRATION_NAME          Maps to subsystemName (e.g. confluence); overridden by CX_SUBSYSTEM_NAME
  BASE_URL                  Atlassian Cloud site URL (`https://tenant.atlassian.net`); hostname used if CONFLUENCE_SITE unset.
  CONFLUENCE_USERNAME       Alias for CONFLUENCE_EMAIL.
  CX_APPLICATION_NAME       Default: Confluence or Jira from audit product, else CORALOGIX_APP_NAME
  CX_SUBSYSTEM_NAME         Default: AuditLog, else INTEGRATION_NAME
  CONFLUENCE_PAGE_LIMIT     Page size (default 100)
  CONFLUENCE_MIN_INTERVAL_SEC  Sleep between Confluence pages (default 0)
  CORALOGIX_BATCH_SIZE      Logs per Coralogix POST (default 50)
  DEBUG                     If ``true``, print resolved settings (secrets redacted)

CLI
---
  --diagnose                Print token-visible sites and probe Confluence ``/user/current`` or Jira ``/myself``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth


def _shell_env_key_ok(key: str) -> bool:
    if not key:
        return False
    for i, ch in enumerate(key):
        if i == 0:
            if not (ch.isalpha() or ch == "_"):
                return False
        elif not (ch.isalnum() or ch == "_"):
            return False
    return True


def _strip_shell_env_value(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if not _shell_env_key_ok(key):
            continue
        out[key] = _strip_shell_env_value(rest)
    return out


def _bootstrap_env_files() -> None:
    """Load ``env.sh``, ``.env``, then ``ENV_FILE`` without overriding OS env."""
    cwd = Path.cwd()
    paths: list[Path] = [cwd / "env.sh", cwd / ".env"]
    extra = os.environ.get("ENV_FILE", "").strip()
    if extra:
        paths.append(Path(extra).expanduser())
    merged: dict[str, str] = {}
    for p in paths:
        rp = p.expanduser()
        try:
            rp = rp.resolve()
        except OSError:
            continue
        if rp.is_file():
            merged.update(_parse_env_file(rp))
    for key, val in merged.items():
        existing = os.environ.get(key)
        if existing is None:
            os.environ[key] = val
            continue
        if isinstance(existing, str) and not existing.strip():
            os.environ[key] = val


def _http_error_body_hint(text: str) -> str:
    """Avoid dumping HTML login pages into tracebacks."""
    sample = text[:800].lower()
    if "<html" in sample or "<!doctype html" in sample:
        return (
            "(HTML response omitted — invalid Atlassian Basic auth or SSO-only flow; "
            "confirm email matches API token owner and token is valid.)"
        )
    return text
def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def _atlassian_api_token() -> str:
    v = os.environ.get("CONFLUENCE_API_TOKEN", "").strip() or os.environ.get(
        "ATLASSIAN_API_TOKEN", ""
    ).strip()
    if not v:
        print(
            "Missing CONFLUENCE_API_TOKEN or ATLASSIAN_API_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)
    return v


def _audit_product() -> str:
    raw = (
        os.environ.get("ATLASSIAN_AUDIT_PRODUCT", "").strip().lower()
        or os.environ.get("AUDIT_PRODUCT", "").strip().lower()
        or "confluence"
    )
    if raw in ("confluence", "wiki"):
        return "confluence"
    if raw in ("jira", "jira-software", "jsm"):
        return "jira"
    print(
        f"Unknown ATLASSIAN_AUDIT_PRODUCT={raw!r}; use confluence or jira.",
        file=sys.stderr,
    )
    sys.exit(1)


def _gateway_cloud_id_confluence() -> str:
    return (
        os.environ.get("CONFLUENCE_CLOUD_ID", "").strip()
        or os.environ.get("ATLASSIAN_CLOUD_ID", "").strip()
    )


def _gateway_cloud_id_jira() -> str:
    return (
        os.environ.get("JIRA_CLOUD_ID", "").strip()
        or os.environ.get("ATLASSIAN_CLOUD_ID", "").strip()
        or os.environ.get("CONFLUENCE_CLOUD_ID", "").strip()
    )


def _utc_today_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_yesterday_date() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")


def _utc_iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _confluence_site_hostname() -> str:
    site = os.environ.get("CONFLUENCE_SITE", "").strip()
    if site:
        return site.removeprefix("https://").split("/")[0]
    base = os.environ.get("BASE_URL", "").strip()
    if base:
        host = urlparse(base).hostname
        if host:
            return host
        return base.removeprefix("https://").split("/")[0]
    return ""


def _atlassian_audit_entry_url(product: str) -> str:
    """Full URL for the first audit API request (Confluence /audit or Jira /auditing/record)."""
    site = _confluence_site_hostname()
    if product == "confluence":
        cid = _gateway_cloud_id_confluence()
        if cid:
            return f"https://api.atlassian.com/ex/confluence/{cid}/wiki/rest/api/audit"
        if not site:
            print(
                "Set CONFLUENCE_SITE or BASE_URL (https://tenant.atlassian.net), "
                "or CONFLUENCE_CLOUD_ID / ATLASSIAN_CLOUD_ID for the gateway URL.",
                file=sys.stderr,
            )
            sys.exit(1)
        return f"https://{site}/wiki/rest/api/audit"
    cid = _gateway_cloud_id_jira()
    if cid:
        return f"https://api.atlassian.com/ex/jira/{cid}/rest/api/3/auditing/record"
    if not site:
        print(
            "Set CONFLUENCE_SITE or BASE_URL (https://tenant.atlassian.net), "
            "or JIRA_CLOUD_ID / ATLASSIAN_CLOUD_ID / CONFLUENCE_CLOUD_ID for the gateway URL.",
            file=sys.stderr,
        )
        sys.exit(1)
    return f"https://{site}/rest/api/3/auditing/record"


def _probe_url_for_audit_product(audit_url: str, product: str) -> str:
    """Lightweight REST check: Confluence ``user/current`` or Jira ``myself``."""
    if product == "confluence":
        if not audit_url.endswith("/audit"):
            raise RuntimeError(f"Unexpected Confluence audit URL: {audit_url!r}")
        base = audit_url[: -len("/audit")]
        return f"{base}/user/current"
    if not audit_url.endswith("/auditing/record"):
        raise RuntimeError(f"Unexpected Jira audit URL: {audit_url!r}")
    base = audit_url[: -len("/auditing/record")]
    return f"{base}/myself"


ATLASSIAN_ACCESSIBLE_RESOURCES = "https://api.atlassian.com/oauth/token/accessible-resources"


def diagnose_atlassian_auth(
    sess: requests.Session,
    auth: HTTPBasicAuth,
    *,
    product: str,
    audit_url: str,
    configured_cloud_id: str,
    configured_site_host: str,
) -> None:
    """Print token-visible Atlassian sites and a product-specific REST probe."""
    print("=== Sites visible to this API token (accessible-resources) ===", file=sys.stderr)
    r = sess.get(
        ATLASSIAN_ACCESSIBLE_RESOURCES,
        auth=auth,
        headers={"Accept": "application/json"},
        timeout=120,
    )
    print(f"HTTP {r.status_code}", file=sys.stderr)
    if r.status_code != 200:
        print(r.text[:4000], file=sys.stderr)
        print(
            "\nIf this is 401: wrong email/token pair, revoked token, or typo in CONFLUENCE_EMAIL / ATLASSIAN_EMAIL.",
            file=sys.stderr,
        )
        return

    try:
        resources = r.json()
    except json.JSONDecodeError:
        print(r.text[:4000], file=sys.stderr)
        return

    if not isinstance(resources, list):
        print(json.dumps(resources, indent=2, default=str)[:8000], file=sys.stderr)
        return

    ids_urls: list[tuple[str, str]] = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        url = item.get("url")
        name = item.get("name")
        if isinstance(rid, str) and isinstance(url, str):
            ids_urls.append((rid, url))
            extra = f" ({name})" if isinstance(name, str) and name else ""
            print(f"  cloudId={rid} url={url}{extra}", file=sys.stderr)

    if configured_cloud_id:
        match = any(rid == configured_cloud_id for rid, _ in ids_urls)
        lbl = "gateway cloud ID"
        print(
            f"\nConfigured {lbl}={configured_cloud_id!r} → "
            f"{'listed above' if match else 'NOT in token site list — wrong cloud ID or token owner'}",
            file=sys.stderr,
        )
    elif configured_site_host:
        norm = configured_site_host.lower().rstrip("/")
        match = any(
            urlparse(u).hostname and urlparse(u).hostname.lower() == norm for _, u in ids_urls
        )
        print(
            f"\nConfigured site host {configured_site_host!r} → "
            f"{'matches a listed URL' if match else 'NOT in token site list — wrong BASE_URL/site or token owner'}",
            file=sys.stderr,
        )

    probe = _probe_url_for_audit_product(audit_url, product)
    label = "Confluence" if product == "confluence" else "Jira"
    print(f"\n=== {label} REST probe ===", file=sys.stderr)
    print(f"GET {probe}", file=sys.stderr)
    r2 = sess.get(probe, auth=auth, headers={"Accept": "application/json"}, timeout=120)
    print(f"HTTP {r2.status_code}", file=sys.stderr)
    print(_http_error_body_hint(r2.text)[:4000], file=sys.stderr)
    if r2.status_code == 403:
        if product == "confluence":
            print(
                "\n403: no Confluence product access on this site, or missing permission. "
                "If you only use Jira on this tenant, set ATLASSIAN_AUDIT_PRODUCT=jira and retry.",
                file=sys.stderr,
            )
        else:
            print(
                "\n403: missing Jira access or Administer Jira global permission (audit requires admin).",
                file=sys.stderr,
            )


def _audit_failure_message(status_code: int, text: str, *, product: str) -> str:
    text = _http_error_body_hint(text)
    api_msg = ""
    try:
        j = json.loads(text)
        if isinstance(j, dict) and isinstance(j.get("message"), str):
            api_msg = j["message"]
    except json.JSONDecodeError:
        pass
    label = "Confluence" if product == "confluence" else "Jira"
    base = f"{label} audit failed {status_code}: {text}"
    if product == "confluence" and status_code == 403 and (
        "not permitted to use Confluence" in text
        or api_msg == "Current user not permitted to use Confluence"
    ):
        return (
            f"{base}\n\n"
            'Atlassian returned "not permitted to use Confluence": this endpoint is Confluence Cloud audit only. '
            "Your account has no Confluence product on this `*.atlassian.net` site (common on Jira-only tenants).\n"
            "Fix options: (1) Add Confluence and Confluence admin for this user, or (2) Use Jira audit instead:\n"
            "  export ATLASSIAN_AUDIT_PRODUCT=jira\n"
            "Jira mode calls GET /rest/api/3/auditing/record and requires Administer Jira.\n"
            "Run: .venv/bin/python main.py --diagnose"
        )
    if product == "jira" and status_code == 403:
        return (
            f"{base}\n\n"
            "Jira audit requires Administer Jira global permission (and API scopes per Atlassian docs).\n"
            "Run: .venv/bin/python main.py --diagnose"
        )
    if status_code == 401:
        return (
            f"{base}\n\n"
            "HTTP 401: Atlassian rejected Basic auth. Regenerate an API token for this exact email "
            "at https://id.atlassian.com/manage-profile/security/api-tokens — scoped/org tokens must "
            "allow Jira or Confluence REST as documented.\n"
            "Run: .venv/bin/python main.py --diagnose"
        )
    return base


def _resolve_coralogix_domain() -> str:
    d = os.environ.get("CORALOGIX_DOMAIN", "").strip()
    if d:
        return d.removeprefix("https://").split("/")[0]
    log_url = os.environ.get("CORALOGIX_LOG_URL", "").strip()
    if log_url:
        host = urlparse(log_url).hostname or ""
        if host.startswith("ingress."):
            return host.removeprefix("ingress.")
        if host:
            return host
    print(
        "Missing CORALOGIX_DOMAIN (e.g. ap3.coralogix.com). "
        "Alternatively set CORALOGIX_LOG_URL to a full ingress URL so the region can be parsed.",
        file=sys.stderr,
    )
    sys.exit(1)


def _lookback_minutes_from_env() -> int | None:
    raw = os.environ.get("INTEGRATION_SEARCH_DIFF_IN_MINUTES", "").strip()
    if not raw:
        raw = os.environ.get("CONFLUENCE_LOOKBACK_MINUTES", "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n > 0 else None


def _resolve_audit_date_range(
    *,
    args_start: str | None,
    args_end: str | None,
) -> tuple[str, str]:
    """Returns (startDate, endDate) query strings for Confluence /audit."""
    cli_start = (args_start or "").strip()
    cli_end = (args_end or "").strip()
    env_start = os.environ.get("CONFLUENCE_START_DATE", "").strip()
    env_end = os.environ.get("CONFLUENCE_END_DATE", "").strip()
    explicit_calendar = bool(cli_start or cli_end or env_start or env_end)

    lookback = _lookback_minutes_from_env()
    if lookback is not None and not explicit_calendar:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=lookback)
        return _utc_iso_z(start), _utc_iso_z(end)

    start_date = cli_start or env_start or _utc_yesterday_date()
    end_date = cli_end or env_end or _utc_today_date()
    return start_date, end_date


def _jira_audit_time_bounds(start_date: str, end_date: str) -> tuple[str, str]:
    """``from`` / ``to`` query parameters for Jira ``/auditing/record``."""

    def calendar_bounds(cal: str, *, end_of_day: bool) -> str:
        if len(cal) == 10 and cal[4] == "-" and cal[7] == "-":
            if end_of_day:
                return f"{cal}T23:59:59.999+0000"
            return f"{cal}T00:00:00.000+0000"
        return cal

    fr = start_date if "T" in start_date else calendar_bounds(start_date, end_of_day=False)
    to = end_date if "T" in end_date else calendar_bounds(end_date, end_of_day=True)
    return fr, to


def _parse_atlassian_created_ms(value: str) -> float | None:
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = s.replace("+0000", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.timestamp() * 1000.0


def _creation_ms(record: dict[str, Any]) -> float:
    v = record.get("creationDate")
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e15:
            return x / 1_000_000.0
        if x > 10_000_000_000:
            return x
        if x > 1e9:
            return x * 1000.0
        return x * 1000.0
    created = record.get("created")
    if isinstance(created, str):
        parsed = _parse_atlassian_created_ms(created)
        if parsed is not None:
            return parsed
    return time.time() * 1000.0


def _record_severity(record: dict[str, Any]) -> int:
    for key in ("summary", "description", "category"):
        s = record.get(key)
        if isinstance(s, str) and s:
            t = s.lower()
            if any(x in t for x in ("delete", "remove", "destroy", "purge")):
                return 4
    return 3


def _record_computer_name(record: dict[str, Any]) -> str:
    ra = record.get("remoteAddress")
    if isinstance(ra, str) and ra.strip():
        return ra.strip()[:1024]
    ak = record.get("authorKey")
    if isinstance(ak, str) and ak.strip():
        return ak.strip()[:1024]
    aid = record.get("authorAccountId")
    if isinstance(aid, str) and aid.strip():
        return aid.strip()[:1024]
    author = record.get("author") if isinstance(record.get("author"), dict) else {}
    for key in ("accountId", "displayName", "username"):
        u = author.get(key)
        if isinstance(u, str) and u.strip():
            return u.strip()[:1024]
    return "atlassian-audit"


def audit_record_to_coralogix(
    record: dict[str, Any],
    *,
    application_name: str,
    subsystem_name: str,
    integration_name: str | None,
    source_key: str,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "source": source_key,
        "record": record,
    }
    if integration_name:
        envelope["integration"] = integration_name
    return {
        "applicationName": application_name,
        "subsystemName": subsystem_name,
        "computerName": _record_computer_name(record),
        "timestamp": _creation_ms(record),
        "severity": _record_severity(record),
        "text": json.dumps(envelope, separators=(",", ":"), default=str),
    }


def send_coralogix_batch(
    sess: requests.Session,
    ingress_url: str,
    private_key: str,
    batch: list[dict[str, Any]],
) -> None:
    if not batch:
        return
    url = f"{ingress_url.rstrip('/')}/logs/v1/singles"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {private_key}",
    }
    r = sess.post(url, headers=headers, data=json.dumps(batch), timeout=120)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Coralogix ingest failed {r.status_code}: {r.text}")


def fetch_confluence_audit_page(
    sess: requests.Session,
    *,
    audit_url: str,
    auth: HTTPBasicAuth,
    start_date: str,
    end_date: str,
    start: int,
    limit: int,
    search_string: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "startDate": start_date,
        "endDate": end_date,
        "start": start,
        "limit": limit,
    }
    if search_string:
        params["searchString"] = search_string
    r = sess.get(
        audit_url,
        auth=auth,
        headers={"Accept": "application/json"},
        params=params,
        timeout=120,
    )
    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else 60.0
        raise RuntimeError(f"Confluence rate limited (429); retry after ~{wait:.0f}s per Retry-After.")
    if r.status_code != 200:
        raise RuntimeError(_audit_failure_message(r.status_code, r.text, product="confluence"))
    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected Confluence JSON shape: {type(body).__name__}")
    return body


def fetch_jira_audit_page(
    sess: requests.Session,
    *,
    audit_url: str,
    auth: HTTPBasicAuth,
    from_dt: str,
    to_dt: str,
    offset: int,
    limit: int,
    filter_q: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "from": from_dt,
        "to": to_dt,
        "offset": offset,
        "limit": limit,
    }
    if filter_q:
        params["filter"] = filter_q
    r = sess.get(
        audit_url,
        auth=auth,
        headers={"Accept": "application/json"},
        params=params,
        timeout=120,
    )
    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else 60.0
        raise RuntimeError(f"Jira rate limited (429); retry after ~{wait:.0f}s per Retry-After.")
    if r.status_code != 200:
        raise RuntimeError(_audit_failure_message(r.status_code, r.text, product="jira"))
    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected Jira JSON shape: {type(body).__name__}")
    return body


def main() -> None:
    _bootstrap_env_files()
    p = argparse.ArgumentParser(
        description="Ship Atlassian Cloud (Confluence or Jira) audit records to Coralogix.",
    )
    p.add_argument(
        "--start-date",
        help="Audit window start (Confluence startDate / Jira from); default from env or ~24h UTC.",
    )
    p.add_argument(
        "--end-date",
        help="Audit window end (Confluence endDate / Jira to); default from env or today UTC.",
    )
    p.add_argument(
        "--search",
        help="Optional filter (Confluence searchString / Jira filter query).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and count records only; do not call Coralogix.",
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="Print token-visible sites and probe Confluence /user/current or Jira /myself; exit.",
    )
    args = p.parse_args()

    product = _audit_product()

    email = (
        os.environ.get("CONFLUENCE_EMAIL", "").strip()
        or os.environ.get("CONFLUENCE_USERNAME", "").strip()
        or os.environ.get("ATLASSIAN_EMAIL", "").strip()
    )
    if not email:
        print(
            "Missing CONFLUENCE_EMAIL, CONFLUENCE_USERNAME, or ATLASSIAN_EMAIL for Basic auth "
            "with CONFLUENCE_API_TOKEN / ATLASSIAN_API_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    token = _atlassian_api_token()
    audit_url = _atlassian_audit_entry_url(product)

    if args.diagnose:
        auth = HTTPBasicAuth(email, token)
        sess = requests.Session()
        gw = (
            _gateway_cloud_id_confluence()
            if product == "confluence"
            else _gateway_cloud_id_jira()
        )
        diagnose_atlassian_auth(
            sess,
            auth,
            product=product,
            audit_url=audit_url,
            configured_cloud_id=gw,
            configured_site_host=_confluence_site_hostname(),
        )
        return

    start_date, end_date = _resolve_audit_date_range(args_start=args.start_date, args_end=args.end_date)

    if not args.dry_run:
        cx_key = _require_env("CORALOGIX_PRIVATE_KEY")
        cx_domain = _resolve_coralogix_domain()
        ingress = f"https://ingress.{cx_domain}"
    else:
        cx_key = ""
        ingress = ""

    default_app = "Jira" if product == "jira" else "Confluence"
    app_name = (
        os.environ.get("CX_APPLICATION_NAME", "").strip()
        or os.environ.get("CORALOGIX_APP_NAME", "").strip()
        or default_app
    )
    sub_name = (
        os.environ.get("CX_SUBSYSTEM_NAME", "").strip()
        or os.environ.get("INTEGRATION_NAME", "").strip()
        or "AuditLog"
    )
    integration_tag = os.environ.get("INTEGRATION_NAME", "").strip() or None

    source_key = "jira_cloud_audit" if product == "jira" else "confluence_cloud_audit"
    search_arg = args.search or ""
    filter_q = (
        search_arg.strip()
        or os.environ.get("JIRA_AUDIT_FILTER", "").strip()
        or os.environ.get("CONFLUENCE_SEARCH", "").strip()
        or None
    )

    if os.environ.get("DEBUG", "").strip().lower() == "true":
        lb = _lookback_minutes_from_env()
        print(
            json.dumps(
                {
                    "debug": True,
                    "audit_product": product,
                    "audit_url": audit_url,
                    "start_date": start_date,
                    "end_date": end_date,
                    "lookback_minutes": lb,
                    "coralogix_ingress": ingress or "(dry-run)",
                    "application_name": app_name,
                    "subsystem_name": sub_name,
                    "atlassian_user": email,
                    "atlassian_token_set": bool(token),
                    "coralogix_key_set": bool(cx_key),
                },
                indent=2,
            ),
            file=sys.stderr,
        )

    raw_limit = int(os.environ.get("CONFLUENCE_PAGE_LIMIT", "100"))
    cap = 1000 if product == "jira" else 500
    page_limit = max(1, min(cap, raw_limit))
    min_interval = float(os.environ.get("CONFLUENCE_MIN_INTERVAL_SEC", "0"))
    cx_batch = int(os.environ.get("CORALOGIX_BATCH_SIZE", "50"))

    auth = HTTPBasicAuth(email, token)
    sess = requests.Session()

    offset = 0
    total = 0
    pending: list[dict[str, Any]] = []

    j_from: str | None = None
    j_to: str | None = None
    if product == "jira":
        j_from, j_to = _jira_audit_time_bounds(start_date, end_date)

    while True:
        if product == "confluence":
            body = fetch_confluence_audit_page(
                sess,
                audit_url=audit_url,
                auth=auth,
                start_date=start_date,
                end_date=end_date,
                start=offset,
                limit=page_limit,
                search_string=filter_q,
            )
            rows = body.get("results") or []
            if not isinstance(rows, list):
                raise RuntimeError("Confluence response missing list results[]")
        else:
            assert j_from is not None and j_to is not None
            body = fetch_jira_audit_page(
                sess,
                audit_url=audit_url,
                auth=auth,
                from_dt=j_from,
                to_dt=j_to,
                offset=offset,
                limit=page_limit,
                filter_q=filter_q,
            )
            rows = body.get("records") or []
            if not isinstance(rows, list):
                raise RuntimeError("Jira response missing list records[]")

        if not rows:
            break

        for item in rows:
            if not isinstance(item, dict):
                continue
            pending.append(
                audit_record_to_coralogix(
                    item,
                    application_name=app_name,
                    subsystem_name=sub_name,
                    integration_name=integration_tag,
                    source_key=source_key,
                )
            )
            total += 1
            if not args.dry_run and len(pending) >= cx_batch:
                send_coralogix_batch(sess, ingress, cx_key, pending)
                print(f"Sent Coralogix batch ({len(pending)} logs); running total records: {total}.")
                pending.clear()

        if product == "jira":
            tot = body.get("total")
            if isinstance(tot, int) and offset + len(rows) >= tot:
                break
            if len(rows) < page_limit:
                break
        elif len(rows) < page_limit:
            break

        offset += len(rows)
        if min_interval > 0:
            time.sleep(min_interval)

    if not args.dry_run and pending:
        send_coralogix_batch(sess, ingress, cx_key, pending)
        print(f"Sent final Coralogix batch ({len(pending)} logs).")

    prod_label = "Jira" if product == "jira" else "Confluence"
    if args.dry_run:
        print(f"Dry run: fetched {total} {prod_label} audit record(s); Coralogix not called.")
    else:
        print(f"Done. Shipped {total} {prod_label} audit record(s) to Coralogix ({app_name}/{sub_name}).")


if __name__ == "__main__":
    main()
