"""Checks every service in services.toml once and updates the status data.

Runs every ~5 minutes in GitHub Actions (outside the hosting it watches) and
needs only the Python standard library. Data files (on the `data` branch):

- current.json  the latest result per component and check
- daily.json    per component and UTC day: checks, up, degraded, summed ms
- events.json   status changes (newest first), for "detected automatically"
- meta.json     when the site was last deployed

Usage: python3 status/probe.py --services services.toml --data <dir>
Prints `changed=true|false` and `deploy=true|false` for $GITHUB_OUTPUT.
"""
from __future__ import annotations

import argparse
import json
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = "deskranger-status/1 (+https://status.deskranger.app)"
TIMEOUT_S = 10
RETRY_PAUSE_S = 5
DEPLOY_EVERY = timedelta(minutes=60)
EVENTS_KEPT = 200
RANK = {"up": 0, "degraded": 1, "down": 2}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer of its own (the apex forwarding is checked by it)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def fetch(url: str) -> tuple[int, str, int]:
    """(HTTP status, start of the body, milliseconds); status 0 when nothing answered."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    started = time.monotonic()
    try:
        with OPENER.open(request, timeout=TIMEOUT_S) as response:
            body = response.read(65536).decode("utf-8", "replace")
            return response.status, body, round((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as error:
        body = error.read(65536).decode("utf-8", "replace") if error.fp else ""
        return error.code, body, round((time.monotonic() - started) * 1000)
    except Exception as error:  # timeouts, DNS, TLS: the service did not answer
        return 0, f"{type(error).__name__}: {error}", round((time.monotonic() - started) * 1000)


def evaluate(check: dict, status: int, body: str, ms: int, degraded_ms: int) -> dict:
    """One check's verdict from what came back."""
    expected = check.get("expect_status", [200])
    if status == 0:
        return {"status": "down", "code": 0, "ms": ms, "error": body[:200]}
    if status not in expected:
        return {"status": "down", "code": status, "ms": ms, "error": f"HTTP {status}"}
    text = check.get("expect_text")
    if text and text not in body:
        return {"status": "down", "code": status, "ms": ms, "error": "unexpected answer"}
    return {"status": "degraded" if ms > degraded_ms else "up", "code": status, "ms": ms}


def run_check(check: dict, degraded_ms: int, fetcher=fetch, pause=time.sleep) -> dict:
    """Checks once and, if that fails, once more after a pause (a single lost packet is no outage)."""
    result = evaluate(check, *fetcher(check["url"]), degraded_ms)
    if result["status"] == "down":
        pause(RETRY_PAUSE_S)
        result = evaluate(check, *fetcher(check["url"]), degraded_ms)
    return result


def worst(statuses: list[str]) -> str:
    return max(statuses, key=RANK.__getitem__) if statuses else "up"


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def update(config: dict, data_dir: Path, now: datetime, results: dict[str, dict[str, dict]]) -> dict:
    """Writes the data files from one round of results; returns what changed."""
    data_dir.mkdir(parents=True, exist_ok=True)
    previous = load(data_dir / "current.json", {}).get("components", {})
    daily = load(data_dir / "daily.json", {})
    events = load(data_dir / "events.json", [])
    meta = load(data_dir / "meta.json", {})
    day = now.strftime("%Y-%m-%d")
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    keep_from = (now - timedelta(days=config["site"]["days"] + 5)).strftime("%Y-%m-%d")

    components = {}
    changed = False
    for component in config["component"]:
        cid = component["id"]
        checks = results[cid]
        status = worst([c["status"] for c in checks.values()])
        ms = max((c["ms"] for c in checks.values()), default=0)
        components[cid] = {"status": status, "ms": ms, "checks": checks}
        bucket = daily.setdefault(cid, {}).setdefault(day, {"n": 0, "up": 0, "degraded": 0, "ms": 0})
        bucket["n"] += 1
        bucket["up"] += status == "up"
        bucket["degraded"] += status == "degraded"
        bucket["ms"] += ms
        daily[cid] = {d: v for d, v in daily[cid].items() if d >= keep_from}
        before = previous.get(cid, {}).get("status")
        if before is not None and before != status:
            changed = True
            events.insert(0, {"component": cid, "from": before, "to": status, "at": stamp})
    events = events[:EVENTS_KEPT]

    last_deploy = meta.get("deployed_at")
    due = last_deploy is None or now - datetime.fromisoformat(last_deploy.replace("Z", "+00:00")) >= DEPLOY_EVERY
    deploy = changed or due
    if deploy:
        meta["deployed_at"] = stamp

    current = {"checked_at": stamp, "status": worst([c["status"] for c in components.values()]), "components": components}
    (data_dir / "current.json").write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    (data_dir / "daily.json").write_text(json.dumps(daily, indent=1, sort_keys=True) + "\n")
    (data_dir / "events.json").write_text(json.dumps(events, indent=1) + "\n")
    (data_dir / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return {"changed": changed, "deploy": deploy}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--services", default="services.toml")
    parser.add_argument("--data", required=True)
    args = parser.parse_args(argv)
    config = tomllib.loads(Path(args.services).read_text())
    degraded_ms = config["site"]["degraded_ms"]
    results = {
        component["id"]: {check["id"]: run_check(check, degraded_ms) for check in component["check"]}
        for component in config["component"]
    }
    outcome = update(config, Path(args.data), datetime.now(timezone.utc), results)
    for cid, checks in results.items():
        for kid, result in checks.items():
            print(f"# {cid}/{kid}: {result['status']} {result.get('code')} {result['ms']} ms {result.get('error', '')}")
    print(f"changed={'true' if outcome['changed'] else 'false'}")
    print(f"deploy={'true' if outcome['deploy'] else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
