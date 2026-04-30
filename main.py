#!/usr/bin/env python3
"""
Pull **Confluence Cloud** site audit records and ship each row to Coralogix via
``POST https://ingress.<domain>/logs/v1/singles``.

Uses the Confluence REST API:

  GET {base}/wiki/rest/api/audit

where ``base`` is either ``https://<your-site>.atlassian.net`` or the gateway
``https://api.atlassian.com/ex/confluence/<cloudId>`` (see ``CONFLUENCE_CLOUD_ID``).

**Authentication:** Confluence Cloud REST uses **HTTP Basic** auth
(Atlassian account **email** + **API token**). There is no supported mode that
uses the API token alone for this endpoint; scoped tokens still use the same
Basic scheme with email.

**Permissions:** the Atlassian user must have **Confluence administrator**
(global). The API token needs access to audit (classic token) or the
``read:audit-log:confluence`` scope (scoped token). Audit is not available on
all plans.

Python
------
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python main.py

Environment
-----------
Required:
  CONFLUENCE_API_TOKEN      API token (id.atlassian.com → Security → API tokens)
  CONFLUENCE_EMAIL          Atlassian account email for Basic auth (or CONFLUENCE_USERNAME)
  CONFLUENCE_SITE           Hostname only, e.g. mycompany.atlassian.net — or set BASE_URL instead
                            (unless CONFLUENCE_CLOUD_ID is set)
  CORALOGIX_PRIVATE_KEY     Send-Your-Data API key
  CORALOGIX_DOMAIN          e.g. ap3.coralogix.com, eu2.coralogix.com (no https://). Optional if
                            CORALOGIX_LOG_URL is set and the host can be parsed.

Optional:
  CONFLUENCE_CLOUD_ID       If set, calls api.atlassian.com/ex/confluence/{id}/... (CONFLUENCE_SITE not needed)
  CONFLUENCE_START_DATE     startDate query (string; often YYYY-MM-DD or full ISO)
  CONFLUENCE_END_DATE       endDate query
  INTEGRATION_SEARCH_DIFF_IN_MINUTES  Rolling window: end=now UTC, start=now−N minutes (ISO strings).
                            Default when unset: calendar CONFLUENCE_*_DATE (~24h by date) or explicit dates.
  CONFLUENCE_LOOKBACK_MINUTES        Alias for the same rolling window if INTEGRATION_SEARCH_DIFF_IN_MINUTES is unset.
  CORALOGIX_LOG_URL         If CORALOGIX_DOMAIN is empty, region is parsed from this URL's hostname
                            (e.g. https://ingress.ap3.coralogix.com/... → ap3.coralogix.com).
  CORALOGIX_APP_NAME        Maps to applicationName; overridden by CX_APPLICATION_NAME
  INTEGRATION_NAME          Maps to subsystemName (e.g. confluence); overridden by CX_SUBSYSTEM_NAME
  BASE_URL                  Full Confluence site URL; hostname used if CONFLUENCE_SITE is unset.
  CONFLUENCE_USERNAME       Alias for CONFLUENCE_EMAIL.
  CX_APPLICATION_NAME       Default: Confluence, else CORALOGIX_APP_NAME
  CX_SUBSYSTEM_NAME         Default: AuditLog, else INTEGRATION_NAME
  CONFLUENCE_PAGE_LIMIT     Page size (default 100)
  CONFLUENCE_MIN_INTERVAL_SEC  Sleep between Confluence pages (default 0)
  CORALOGIX_BATCH_SIZE      Logs per Coralogix POST (default 50)
  DEBUG                     If ``true``, print resolved settings (secrets redacted)

CLI
---
  --diagnose                Print sites visible to the API token and probe ``GET .../user/current``
                            (helps when audit worked before but now returns 403).
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

import requests
from requests.auth import HTTPBasicAuth


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


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


def _audit_url() -> str:
    cloud_id = os.environ.get("CONFLUENCE_CLOUD_ID", "").strip()
    if cloud_id:
        return f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api/audit"
    site = _confluence_site_hostname()
    if not site:
        print(
            "Set CONFLUENCE_SITE (hostname) or BASE_URL (https://tenant.atlassian.net), "
            "or CONFLUENCE_CLOUD_ID for the gateway URL.",
            file=sys.stderr,
        )
        sys.exit(1)
    return f"https://{site}/wiki/rest/api/audit"


def _confluence_rest_api_base(audit_url: str) -> str:
    """``.../wiki/rest/api`` prefix for the configured site or gateway URL."""
    suffix = "/audit"
    if not audit_url.endswith(suffix):
        raise RuntimeError(f"Unexpected audit URL shape: {audit_url!r}")
    return audit_url[: -len(suffix)]


ATLASSIAN_ACCESSIBLE_RESOURCES = "https://api.atlassian.com/oauth/token/accessible-resources"


def diagnose_confluence_auth(
    sess: requests.Session,
    auth: HTTPBasicAuth,
    *,
    audit_url: str,
    configured_cloud_id: str,
    configured_site_host: str,
) -> None:
    """Print token-visible Atlassian sites and a lightweight Confluence REST probe."""
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
            "\nIf this is 401: wrong email/token pair, revoked token, or typo in CONFLUENCE_EMAIL.",
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
        print(
            f"\nConfigured CONFLUENCE_CLOUD_ID={configured_cloud_id!r} → "
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

    base = _confluence_rest_api_base(audit_url)
    probe = f"{base}/user/current"
    print("\n=== Confluence REST probe (expect 200 if product access + permission) ===", file=sys.stderr)
    print(f"GET {probe}", file=sys.stderr)
    r2 = sess.get(probe, auth=auth, headers={"Accept": "application/json"}, timeout=120)
    print(f"HTTP {r2.status_code}", file=sys.stderr)
    print(r2.text[:4000], file=sys.stderr)
    if r2.status_code == 403:
        print(
            "\n403 here matches audit 403: account lost Confluence on this site "
            "(license, group, SCIM, admin removed product) or wrong site for this token.",
            file=sys.stderr,
        )


def _audit_failure_message(status_code: int, text: str) -> str:
    api_msg = ""
    try:
        j = json.loads(text)
        if isinstance(j, dict) and isinstance(j.get("message"), str):
            api_msg = j["message"]
    except json.JSONDecodeError:
        pass
    base = f"Confluence audit failed {status_code}: {text}"
    if status_code == 403 and (
        "not permitted to use Confluence" in text or api_msg == "Current user not permitted to use Confluence"
    ):
        return (
            f"{base}\n\n"
            "Atlassian returned \"not permitted to use Confluence\": the authenticated account no longer has "
            "Confluence product access on this site (common after license/group/SCIM changes), or BASE_URL / "
            "CONFLUENCE_CLOUD_ID does not match the organization where this user has Confluence.\n"
            "Verify the same user can open Confluence in the browser for this site; confirm "
            "CONFLUENCE_EMAIL matches the API token owner.\n"
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
    author = record.get("author") if isinstance(record.get("author"), dict) else {}
    for key in ("accountId", "displayName", "username"):
        u = author.get(key)
        if isinstance(u, str) and u.strip():
            return u.strip()[:1024]
    return "confluence-audit"


def audit_record_to_coralogix(
    record: dict[str, Any],
    *,
    application_name: str,
    subsystem_name: str,
    integration_name: str | None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "source": "confluence_cloud_audit",
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
        raise RuntimeError(_audit_failure_message(r.status_code, r.text))
    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected Confluence JSON shape: {type(body).__name__}")
    return body


def main() -> None:
    p = argparse.ArgumentParser(description="Ship Confluence Cloud audit records to Coralogix.")
    p.add_argument(
        "--start-date",
        help="startDate for /audit (default: CONFLUENCE_START_DATE or ~24h ago UTC date)",
    )
    p.add_argument(
        "--end-date",
        help="endDate for /audit (default: CONFLUENCE_END_DATE or today UTC date)",
    )
    p.add_argument("--search", help="Optional searchString filter.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and count records only; do not call Coralogix.",
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="Print sites visible to the token and probe GET .../wiki/rest/api/user/current; exit.",
    )
    args = p.parse_args()

    email = (
        os.environ.get("CONFLUENCE_EMAIL", "").strip()
        or os.environ.get("CONFLUENCE_USERNAME", "").strip()
        or os.environ.get("ATLASSIAN_EMAIL", "").strip()
    )
    if not email:
        print(
            "Missing CONFLUENCE_EMAIL, CONFLUENCE_USERNAME, or ATLASSIAN_EMAIL for Basic auth "
            "with CONFLUENCE_API_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    token = _require_env("CONFLUENCE_API_TOKEN")
    audit_url = _audit_url()

    if args.diagnose:
        auth = HTTPBasicAuth(email, token)
        sess = requests.Session()
        diagnose_confluence_auth(
            sess,
            auth,
            audit_url=audit_url,
            configured_cloud_id=os.environ.get("CONFLUENCE_CLOUD_ID", "").strip(),
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

    app_name = (
        os.environ.get("CX_APPLICATION_NAME", "").strip()
        or os.environ.get("CORALOGIX_APP_NAME", "").strip()
        or "Confluence"
    )
    sub_name = (
        os.environ.get("CX_SUBSYSTEM_NAME", "").strip()
        or os.environ.get("INTEGRATION_NAME", "").strip()
        or "AuditLog"
    )
    integration_tag = os.environ.get("INTEGRATION_NAME", "").strip() or None

    if os.environ.get("DEBUG", "").strip().lower() == "true":
        lb = _lookback_minutes_from_env()
        print(
            json.dumps(
                {
                    "debug": True,
                    "audit_url": audit_url,
                    "start_date": start_date,
                    "end_date": end_date,
                    "lookback_minutes": lb,
                    "coralogix_ingress": ingress or "(dry-run)",
                    "application_name": app_name,
                    "subsystem_name": sub_name,
                    "confluence_user": email,
                    "confluence_token_set": bool(token),
                    "coralogix_key_set": bool(cx_key),
                },
                indent=2,
            ),
            file=sys.stderr,
        )

    raw_limit = int(os.environ.get("CONFLUENCE_PAGE_LIMIT", "100"))
    page_limit = max(1, min(500, raw_limit))
    min_interval = float(os.environ.get("CONFLUENCE_MIN_INTERVAL_SEC", "0"))
    cx_batch = int(os.environ.get("CORALOGIX_BATCH_SIZE", "50"))

    auth = HTTPBasicAuth(email, token)
    sess = requests.Session()

    offset = 0
    total = 0
    pending: list[dict[str, Any]] = []

    while True:
        body = fetch_confluence_audit_page(
            sess,
            audit_url=audit_url,
            auth=auth,
            start_date=start_date,
            end_date=end_date,
            start=offset,
            limit=page_limit,
            search_string=(args.search or os.environ.get("CONFLUENCE_SEARCH", "").strip() or None),
        )
        rows = body.get("results") or []
        if not isinstance(rows, list):
            raise RuntimeError("Confluence response missing list results[]")

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
                )
            )
            total += 1
            if not args.dry_run and len(pending) >= cx_batch:
                send_coralogix_batch(sess, ingress, cx_key, pending)
                print(f"Sent Coralogix batch ({len(pending)} logs); running total records: {total}.")
                pending.clear()

        if len(rows) < page_limit:
            break
        offset += len(rows)
        if min_interval > 0:
            time.sleep(min_interval)

    if not args.dry_run and pending:
        send_coralogix_batch(sess, ingress, cx_key, pending)
        print(f"Sent final Coralogix batch ({len(pending)} logs).")

    if args.dry_run:
        print(f"Dry run: fetched {total} audit record(s); Coralogix not called.")
    else:
        print(f"Done. Shipped {total} audit record(s) to Coralogix ({app_name}/{sub_name}).")


if __name__ == "__main__":
    main()
