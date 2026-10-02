"""Security checks shown at /security/ (DeskRanger ADR 0046).

Two sources write to the `security-data` branch:
  web.json   the website checks below, run here weekly (.github/workflows/security.yml);
  code.json  the code checks of the private DeskRanger repository (its security workflow writes
             only check, passed, checked and last_passed through the GitHub API).

Publication rule: a check appears once it has passed. A check that fails shows the date it last
passed and nothing else; findings are fixed before anything about them is published.

Usage: python3 status/security.py --data <security-data dir> --testssl <path to testssl.sh>
Standard library only.
"""
from __future__ import annotations

import argparse
import html
import json
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST = "www.deskranger.app"
esc = html.escape

# id, source, name and description per language. The order is the order on the page.
CHECKS = [
    ("observatory", "web",
     {"de": "Sicherheits-Header der Website", "en": "Website security headers"},
     {"de": "Mozilla HTTP Observatory bewertet www.deskranger.app: Content-Security-Policy, HSTS, Einbettung und mehr. Bestanden ab Note A.",
      "en": "Mozilla HTTP Observatory grades www.deskranger.app: Content Security Policy, HSTS, framing and more. Passed from grade A."}),
    ("tls", "web",
     {"de": "Verschlüsselung der Website (TLS)", "en": "Website encryption (TLS)"},
     {"de": "testssl.sh prüft Protokolle, Schlüsselaustausch und Verfahren von www.deskranger.app. Bestanden ab Note A ohne schwere Befunde.",
      "en": "testssl.sh checks the protocols, key exchange and ciphers of www.deskranger.app. Passed from grade A without severe findings."}),
    ("security_txt", "web",
     {"de": "Meldeweg für Sicherheitslücken", "en": "Vulnerability reporting"},
     {"de": "/.well-known/security.txt ist erreichbar, nennt einen Kontakt und gilt noch mindestens 30 Tage (RFC 9116).",
      "en": "/.well-known/security.txt is reachable, names a contact and stays valid for at least 30 more days (RFC 9116)."}),
    ("rust_deps", "code",
     {"de": "Rust-Abhängigkeiten", "en": "Rust dependencies"},
     {"de": "cargo-deny gleicht alle Rust-Pakete mit der RustSec-Datenbank bekannter Sicherheitslücken ab.",
      "en": "cargo-deny matches every Rust package against the RustSec database of known vulnerabilities."}),
    ("npm_deps", "code",
     {"de": "npm-Abhängigkeiten", "en": "npm dependencies"},
     {"de": "npm audit prüft die Pakete der Web-Konsole und des Kontodienstes auf bekannte Lücken (hoch und kritisch).",
      "en": "npm audit checks the packages of the web console and the account service for known vulnerabilities (high and critical)."}),
    ("secrets", "code",
     {"de": "Keine Zugangsdaten im Code", "en": "No credentials in the code"},
     {"de": "gitleaks durchsucht die gesamte Git-Historie nach Schlüsseln, Passwörtern und Tokens.",
      "en": "gitleaks searches the whole Git history for keys, passwords and tokens."}),
    ("code_analysis", "code",
     {"de": "Statische Code-Analyse", "en": "Static code analysis"},
     {"de": "Semgrep prüft den Code mit seinen Standardregeln auf typische Sicherheitsfehler.",
      "en": "Semgrep checks the code for common security mistakes with its default rules."}),
    ("containers", "code",
     {"de": "Container und Abläufe", "en": "Containers and workflows"},
     {"de": "Trivy prüft Dockerfiles und Konfiguration auf riskante Einstellungen, etwa Dienste mit Root-Rechten.",
      "en": "Trivy checks Dockerfiles and configuration for risky settings, such as services running as root."}),
]

TEXT = {
    "de": {
        "title": "Sicherheitsprüfungen", "back": "Status",
        "intro": "Wir prüfen DeskRanger jede Woche selbst und automatisch. Hier steht, welche Prüfungen bestanden sind und wann.",
        "web": "Website", "code": "Code und Abhängigkeiten",
        "passed": "Bestanden", "checked": "geprüft am {d}", "last": "Zuletzt bestanden am {d}",
        "rule_title": "Was wir veröffentlichen",
        "rule": "Schlägt eine Prüfung fehl, beheben wir das zuerst. Solange ein Befund offen ist, veröffentlichen wir keine Details; hier steht dann, wann die Prüfung zuletzt bestanden wurde. Begründete Ausnahmen, etwa eine Lücke in einer Bibliothek, deren betroffenen Teil wir nachweislich nicht nutzen, dokumentieren wir im Code.",
        "report": "Sicherheitslücke gefunden?", "none": "Noch keine Ergebnisse.", "other": "English",
        "fmt_day": "%d.%m.%Y",
    },
    "en": {
        "title": "Security checks", "back": "Status",
        "intro": "We check DeskRanger ourselves, automatically, every week. This page shows which checks passed and when.",
        "web": "Website", "code": "Code and dependencies",
        "passed": "Passed", "checked": "checked {d}", "last": "Last passed {d}",
        "rule_title": "What we publish",
        "rule": "When a check fails, we fix it first. While a finding is open we publish no details; this page then shows when the check last passed. Justified exceptions, such as a vulnerability in a library part we demonstrably do not use, are documented in the code.",
        "report": "Found a vulnerability?", "none": "No results yet.", "other": "Deutsch",
        "fmt_day": "%Y-%m-%d",
    },
}


def merge(previous: dict, results: dict, now: str) -> dict:
    """New web.json: a failed check keeps the date it last passed; `detail` is a public grade only."""
    old = previous.get("checks", {})
    checks = {}
    for cid, (passed, detail) in results.items():
        checks[cid] = {"passed": passed, "checked": now, "detail": detail if passed else "",
                       "last_passed": now if passed else old.get(cid, {}).get("last_passed"),
                       "last_detail": detail if passed else old.get(cid, {}).get("last_detail", "")}
    return {"updated": now, "checks": checks}


def observatory(host: str = HOST) -> tuple[bool, str]:
    request = urllib.request.Request(f"https://observatory-api.mdn.mozilla.net/api/v2/scan?host={host}", method="POST",
                                     headers={"User-Agent": "deskranger-status"})
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read())
    grade, score = result.get("grade") or "", result.get("score")
    return grade.startswith("A"), f"{grade} ({score}/100)" if grade else ""


def tls(testssl: str, host: str = HOST) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "testssl.json"
        subprocess.run([testssl, "--quiet", "--warnings", "off", "--jsonfile", str(out), host],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900, check=False)
        findings = json.loads(out.read_text()) if out.exists() else []
    grade = next((f["finding"] for f in findings if f.get("id") == "overall_grade"), "")
    severe = [f for f in findings if f.get("severity") in ("HIGH", "CRITICAL")]
    return grade in ("A+", "A") and not severe, grade


def parse_security_txt(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            name, value = line.split(":", 1)
            fields.setdefault(name.strip().lower(), []).append(value.strip())
    return fields


def security_txt_ok(text: str, now: datetime) -> bool:
    fields = parse_security_txt(text)
    try:
        expires = datetime.fromisoformat(fields["expires"][0].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    return bool(fields.get("contact")) and expires > now + timedelta(days=30)


def security_txt(host: str = HOST) -> tuple[bool, str]:
    request = urllib.request.Request(f"https://{host}/.well-known/security.txt", headers={"User-Agent": "deskranger-status"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode()
    return security_txt_ok(text, datetime.now(timezone.utc)), ""


def run(testssl: str) -> dict[str, tuple[bool, str]]:
    results = {}
    for cid, check in (("observatory", observatory), ("tls", lambda: tls(testssl)), ("security_txt", security_txt)):
        try:
            results[cid] = check()
        except Exception as error:  # an unreachable service fails the check, it does not stop the others
            print(f"{cid}: {type(error).__name__}: {error}")
            results[cid] = (False, "")
        print(f"{cid}: {'passed' if results[cid][0] else 'not passed'} {results[cid][1]}")
    return results


def day(value: str | None, lang: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(TEXT[lang]["fmt_day"]) if value else ""


def render(web: dict, code: dict, lang: str, prefix: str, other_href: str) -> str:
    t = TEXT[lang]
    sections = []
    for source, data in (("web", web), ("code", code)):
        items = []
        for cid, src, name, description in CHECKS:
            entry = data.get("checks", {}).get(cid)
            if src != source or not entry or not entry.get("last_passed"):
                continue  # only checks that have passed appear
            if entry.get("passed"):
                detail = f' · {esc(entry["detail"])}' if entry.get("detail") else ""
                state = (f'<span class="check-state ok">{t["passed"]}</span>'
                         f'<span class="check-when">{t["checked"].format(d=day(entry["checked"], lang))}{detail}</span>')
            else:
                state = f'<span class="check-state last">{t["last"].format(d=day(entry["last_passed"], lang))}</span>'
            items.append(f'<li><div><h3>{esc(name[lang])}</h3><p>{esc(description[lang])}</p></div><div class="check-status">{state}</div></li>')
        if items:
            sections.append(f'<section class="checks"><h2>{t[source]}</h2><ul>{"".join(items)}</ul></section>')
    body = "".join(sections) or f'<p class="note">{t["none"]}</p>'
    report = f"https://www.deskranger.app/{lang}/security/#report"
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t['title']} · DeskRanger Status</title>
<meta name="description" content="{esc(t['intro'])}">
<link rel="icon" href="{prefix}favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
<header class="top">
  <div class="wrap top-row">
    <a class="brand" href="https://www.deskranger.app/{lang}/"><img src="{prefix}brand.svg" alt="" width="28" height="28">DeskRanger <span>Status</span></a>
    <nav><a href="{prefix}{'' if lang == 'de' else 'en/'}">{t['back']}</a><a href="{other_href}" hreflang="{'en' if lang == 'de' else 'de'}">{t['other']}</a></nav>
  </div>
</header>
<main class="wrap">
  <h1 class="page-title">{t['title']}</h1>
  <p class="lead">{t['intro']}</p>
  {body}
  <section class="how"><h2>{t['rule_title']}</h2><p>{t['rule']}</p>
    <p class="links"><a href="{report}">{t['report']}</a></p>
  </section>
</main>
</body>
</html>
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="checkout of the security-data branch")
    parser.add_argument("--testssl", required=True, help="path to testssl.sh")
    args = parser.parse_args(argv)
    path = Path(args.data) / "web.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(merge(previous, run(args.testssl), now), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
