"""Builds the static status site from services.toml, the probe data and incidents/.

Output (German at /, English at /en/):
  index.html, en/index.html   the page, complete without JavaScript
  feed.xml                    Atom feed of incidents and detected outages
  status.json                 current state and 90-day uptime per component
  style.css, app.js, brand.svg, favicon.svg

Usage: python3 status/build.py --services services.toml --data <dir> --out _site
Standard library only.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
BERLIN = ZoneInfo("Europe/Berlin")
RANK = {"up": 0, "degraded": 1, "maintenance": 1, "down": 2}
RAW = "https://raw.githubusercontent.com/{repo}/data/current.json"
esc = html.escape

TEXT = {
    "de": {
        "lang": "de", "title": "DeskRanger Status", "website": "Website", "help": "Hilfe",
        "all_up": "Alle Dienste laufen", "some_degraded": "Einige Dienste sind langsam",
        "some_down": "Störung bei einem Teil der Dienste", "maintenance": "Wartung läuft",
        "checked": "Zuletzt geprüft", "no_data": "Noch keine Daten", "test": "Testbetrieb",
        "test_note": "Web-Konsole, Konto, Verbindungsaufbau und Relay laufen derzeit als Testumgebung für die Vorabversionen der Apps. Mit dem Start der Produktivdienste kommen sie hier dazu.",
        "up": "Läuft", "degraded": "Langsam", "down": "Gestört", "unknown": "Keine Daten",
        "uptime": "Verfügbarkeit", "days": "{n} Tage", "since": "Messung seit {d}", "today": "Heute",
        "day_tip": "{d}: {p} verfügbar, {n} Prüfungen", "day_none": "{d}: keine Daten",
        "active": "Aktuelle Meldungen", "history": "Vergangene Meldungen", "none": "In den letzten {n} Tagen gab es keine Meldungen.",
        "auto": "automatisch erkannt", "auto_title": "{c} nicht erreichbar", "auto_slow": "{c} langsam",
        "ongoing": "dauert an", "minutes": "{n} Min.", "impact_minor": "Teilstörung", "impact_major": "Störung",
        "impact_maintenance": "Wartung", "investigating": "Wird untersucht", "identified": "Ursache gefunden",
        "monitoring": "Wird beobachtet", "resolved": "Behoben", "scheduled": "Geplant",
        "how_title": "So messen wir",
        "how": "Etwa alle fünf Minuten ruft ein Prüfauftrag bei GitHub (außerhalb unseres Hostings) jeden Dienst auf. Antwortet ein Dienst nicht oder falsch, versucht er es nach fünf Sekunden noch einmal; erst dann zählt die Prüfung als gestört. Antworten über {ms} ms zählen als langsam. Die Daten und der Code dieser Seite sind öffentlich.",
        "feed": "Meldungen als Feed", "source": "Daten und Code", "other": "English",
        "fmt_day": "%d.%m.%Y", "fmt_time": "%d.%m.%Y, %H:%M Uhr",
    },
    "en": {
        "lang": "en", "title": "DeskRanger Status", "website": "Website", "help": "Help",
        "all_up": "All services are running", "some_degraded": "Some services are slow",
        "some_down": "Some services are disrupted", "maintenance": "Maintenance in progress",
        "checked": "Last checked", "no_data": "No data yet", "test": "Test operation",
        "test_note": "Web console, account, connection service and relay currently run as the test environment for the apps' preview builds. The production services will join here at launch.",
        "up": "Running", "degraded": "Slow", "down": "Disrupted", "unknown": "No data",
        "uptime": "Availability", "days": "{n} days", "since": "Measured since {d}", "today": "Today",
        "day_tip": "{d}: {p} available, {n} checks", "day_none": "{d}: no data",
        "active": "Current notices", "history": "Past notices", "none": "No notices in the last {n} days.",
        "auto": "detected automatically", "auto_title": "{c} unreachable", "auto_slow": "{c} slow",
        "ongoing": "ongoing", "minutes": "{n} min", "impact_minor": "Partial disruption", "impact_major": "Disruption",
        "impact_maintenance": "Maintenance", "investigating": "Investigating", "identified": "Identified",
        "monitoring": "Monitoring", "resolved": "Resolved", "scheduled": "Scheduled",
        "how_title": "How we measure",
        "how": "About every five minutes a job at GitHub (outside our hosting) calls every service. If a service does not answer or answers wrongly, it tries again after five seconds; only then the check counts as disrupted. Answers slower than {ms} ms count as slow. The data and the code of this page are public.",
        "feed": "Notices as a feed", "source": "Data and code", "other": "Deutsch",
        "fmt_day": "%Y-%m-%d", "fmt_time": "%Y-%m-%d %H:%M %Z",
    },
}


@dataclass
class Incident:
    """A notice written by hand (incidents/*.toml) or derived from status changes."""
    key: str
    title: dict[str, str]
    impact: str
    components: list[str]
    started: datetime
    resolved: datetime | None
    updates: list[dict] = field(default_factory=list)
    auto: bool = False

    @property
    def active(self) -> bool:
        return self.resolved is None


def aware(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=BERLIN)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_incidents(folder: Path) -> list[Incident]:
    incidents = []
    for path in sorted(folder.glob("*.toml")):
        raw = tomllib.loads(path.read_text())
        updates = sorted(raw.get("update", []), key=lambda u: aware(u["at"]), reverse=True)
        resolved = next((aware(u["at"]) for u in updates if u["status"] == "resolved"), None)
        started = aware(raw.get("started", updates[-1]["at"] if updates else datetime.now(timezone.utc)))
        incidents.append(Incident(
            key=path.stem, title={"de": raw["title"], "en": raw.get("title_en", raw["title"])},
            impact=raw.get("impact", "minor"), components=raw.get("components", []),
            started=started, resolved=resolved, updates=updates,
        ))
    return incidents


def detected(events: list[dict], names: dict[str, dict[str, str]]) -> list[Incident]:
    """Turns status changes into notices: from leaving "up" until it is "up" again."""
    found = []
    open_since: dict[str, tuple[datetime, str]] = {}
    for event in sorted(events, key=lambda e: e["at"]):
        cid, at = event["component"], aware(event["at"])
        if event["from"] == "up" and event["to"] != "up":
            open_since[cid] = (at, event["to"])
        elif event["to"] == "up" and cid in open_since:
            start, kind = open_since.pop(cid)
            found.append((cid, start, at, kind))
    for cid, (start, kind) in open_since.items():
        found.append((cid, start, None, kind))
    incidents = []
    for cid, start, end, kind in found:
        name = names.get(cid, {"de": cid, "en": cid})
        key = "auto_title" if kind == "down" else "auto_slow"
        incidents.append(Incident(
            key=f"auto-{cid}-{start:%Y%m%d%H%M}",
            title={lang: TEXT[lang][key].format(c=name[lang]) for lang in TEXT},
            impact="major" if kind == "down" else "minor", components=[cid],
            started=start, resolved=end, auto=True,
        ))
    return incidents


def day_cells(daily: dict, today: date, days: int) -> list[tuple[date, dict | None]]:
    return [(today - timedelta(days=offset), daily.get((today - timedelta(days=offset)).isoformat()))
            for offset in range(days - 1, -1, -1)]


def availability(bucket: dict) -> float:
    return (bucket["up"] + bucket["degraded"]) / bucket["n"] if bucket["n"] else 1.0


def uptime(daily: dict) -> float | None:
    checks = sum(b["n"] for b in daily.values())
    return None if checks == 0 else sum(b["up"] + b["degraded"] for b in daily.values()) / checks


def percent(value: float | None, lang: str) -> str:
    if value is None:
        return "–"
    text = f"{value * 100:.2f}".rstrip("0").rstrip(".")
    return (text.replace(".", ",") if lang == "de" else text) + " %"


def overall(current: dict, incidents: list[Incident]) -> str:
    states = [c["status"] for c in current.get("components", {}).values()]
    for incident in incidents:
        if incident.active and not incident.auto:
            states.append("maintenance" if incident.impact == "maintenance" else
                          "down" if incident.impact == "major" else "degraded")
    if not states:
        return "unknown"
    top = max(states, key=RANK.__getitem__)
    if top == "maintenance" and "down" not in states:
        return "maintenance"
    return top


def when(value: datetime, lang: str, fmt_key: str = "fmt_time") -> str:
    return value.astimezone(BERLIN).strftime(TEXT[lang][fmt_key])


def render_page(config: dict, current: dict, daily: dict, incidents: list[Incident], lang: str, today: date, prefix: str) -> str:
    t = TEXT[lang]
    days = config["site"]["days"]
    state = overall(current, incidents) if current else "unknown"
    banner = {"up": t["all_up"], "degraded": t["some_degraded"], "down": t["some_down"],
              "maintenance": t["maintenance"], "unknown": t["no_data"]}[state]
    checked = when(aware(current["checked_at"]), lang) if current.get("checked_at") else "–"
    first_day = min((d for comp in daily.values() for d in comp), default=None)

    rows = []
    for component in config["component"]:
        cid = component["id"]
        comp_daily = daily.get(cid, {})
        status = current.get("components", {}).get(cid, {}).get("status", "unknown")
        cells = []
        for day, bucket in day_cells(comp_daily, today, days):
            label_day = day.strftime(t["fmt_day"])
            if bucket is None:
                cells.append(f'<i class="none" title="{esc(t["day_none"].format(d=label_day))}"></i>')
                continue
            share = availability(bucket)
            level = "ok" if share >= 0.999 else "minor" if share >= 0.99 else "major"
            tip = t["day_tip"].format(d=label_day, p=percent(share, lang), n=bucket["n"])
            cells.append(f'<i class="{level}" title="{esc(tip)}"></i>')
        name = component["name"] if lang == "de" else component.get("name_en", component["name"])
        description = component["description"] if lang == "de" else component.get("description_en", component["description"])
        stage = f'<span class="stage">{t["test"]}</span>' if component.get("stage") == "test" else ""
        rows.append(f"""
      <li class="component" data-component="{esc(cid)}">
        <div class="component-head">
          <div class="component-name"><h3>{esc(name)}</h3>{stage}</div>
          <span class="state {status}" data-state>{t.get(status, t["unknown"])}</span>
        </div>
        <p class="component-text">{esc(description)}</p>
        <div class="bars" role="img" aria-label="{esc(t['uptime'])} {esc(t['days'].format(n=days))}: {percent(uptime(comp_daily), lang)}">{''.join(cells)}</div>
        <div class="bars-legend"><span>{esc(t['days'].format(n=days))}</span><span>{t['uptime']} {percent(uptime(comp_daily), lang)}</span><span>{t['today']}</span></div>
      </li>""")

    names = {c["id"]: (c["name"] if lang == "de" else c.get("name_en", c["name"])) for c in config["component"]}
    horizon = datetime.now(timezone.utc) - timedelta(days=days)
    active = [i for i in incidents if i.active]
    past = [i for i in incidents if not i.active and i.started >= horizon]

    def incident_html(incident: Incident) -> str:
        span = ""
        if incident.resolved:
            minutes = max(1, round((incident.resolved - incident.started).total_seconds() / 60))
            span = f"{when(incident.started, lang)} – {incident.resolved.astimezone(BERLIN):%H:%M} · {t['minutes'].format(n=minutes)}"
        else:
            span = f"{when(incident.started, lang)} · {t['ongoing']}"
        affected = ", ".join(esc(names.get(c, c)) for c in incident.components)
        tag = f'<span class="tag auto">{t["auto"]}</span>' if incident.auto else f'<span class="tag {incident.impact}">{t["impact_" + incident.impact]}</span>'
        updates = "".join(
            f'<li><b>{t.get(u["status"], u["status"])}</b> · <time>{when(aware(u["at"]), lang)}</time><p>{esc(u.get("text_en", u["text"]) if lang == "en" else u["text"])}</p></li>'
            for u in incident.updates)
        updates = f'<ol class="updates">{updates}</ol>' if updates else ""
        return (f'<article class="incident" id="{esc(incident.key)}"><div class="incident-head">{tag}'
                f'<h3>{esc(incident.title[lang])}</h3></div><p class="incident-meta">{span} · {affected}</p>{updates}</article>')

    active_html = "".join(incident_html(i) for i in sorted(active, key=lambda i: i.started, reverse=True))
    past_html = "".join(incident_html(i) for i in sorted(past, key=lambda i: i.started, reverse=True)) \
        or f'<p class="muted">{t["none"].format(n=days)}</p>'
    other_href = "en/" if lang == "de" else "../"
    since = f'<p class="muted since">{t["since"].format(d=datetime.fromisoformat(first_day).strftime(t["fmt_day"]))}</p>' if first_day else ""
    how = t["how"].format(ms=config["site"]["degraded_ms"])
    repo = config["site"]["repo"]
    labels = {k: t[k] for k in ("up", "degraded", "down", "unknown", "all_up", "some_degraded", "some_down", "maintenance", "no_data")}
    incident_floor = overall({"components": {}}, active) if active else "up"

    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{t['title']}</title>
<meta name="description" content="{esc(banner)} · {esc(t['checked'])} {esc(checked)}">
<link rel="alternate" hreflang="de" href="{config['site']['url']}/">
<link rel="alternate" hreflang="en" href="{config['site']['url']}/en/">
<link rel="alternate" type="application/atom+xml" href="{prefix}feed.xml" title="{t['feed']}">
<link rel="icon" href="{prefix}favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{prefix}style.css">
<script defer src="{prefix}app.js"></script>
</head>
<body>
<header class="top">
  <div class="wrap top-row">
    <a class="brand" href="https://www.deskranger.app/{lang}/"><img src="{prefix}brand.svg" alt="" width="28" height="28">DeskRanger <span>Status</span></a>
    <nav><a href="https://www.deskranger.app/{lang}/">{t['website']}</a><a href="{other_href}" hreflang="{'en' if lang == 'de' else 'de'}">{t['other']}</a></nav>
  </div>
</header>
<main class="wrap" data-raw="{esc(RAW.format(repo=repo))}" data-labels="{esc(json.dumps(labels))}" data-floor="{incident_floor}" data-lang="{lang}">
  <section class="banner {state}" data-banner>
    <span class="dot" aria-hidden="true"></span>
    <div><h1 data-banner-text>{banner}</h1><p>{t['checked']}: <time data-checked>{checked}</time></p></div>
  </section>
  {f'<section class="incidents active"><h2>{t["active"]}</h2>{active_html}</section>' if active_html else ''}
  <p class="note">{t['test_note']}</p>
  <ul class="components">{''.join(rows)}
  </ul>
  {since}
  <section class="incidents"><h2>{t['history']}</h2>{past_html}</section>
  <section class="how"><h2>{t['how_title']}</h2><p>{how}</p>
    <p class="links"><a href="{prefix}feed.xml">{t['feed']}</a><a href="https://github.com/{repo}">{t['source']}</a><a href="{prefix}status.json">status.json</a></p>
  </section>
</main>
</body>
</html>
"""


def render_feed(config: dict, incidents: list[Incident]) -> str:
    url = config["site"]["url"]
    entries = []
    for incident in sorted(incidents, key=lambda i: i.started, reverse=True)[:50]:
        updated = (incident.updates[0]["at"] if incident.updates else incident.resolved or incident.started)
        summary = " · ".join(f'{TEXT["de"].get(u["status"], u["status"])}: {u["text"]}' for u in incident.updates) \
            or (TEXT["de"]["resolved"] if incident.resolved else TEXT["de"]["ongoing"])
        entries.append(f"""  <entry>
    <id>{url}/#{esc(incident.key)}</id>
    <title>{esc(incident.title['de'])}</title>
    <link href="{url}/#{esc(incident.key)}"/>
    <updated>{aware(updated).isoformat()}</updated>
    <summary>{esc(summary)}</summary>
  </entry>""")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>{url}/</id>
  <title>DeskRanger Status</title>
  <link href="{url}/"/>
  <link rel="self" href="{url}/feed.xml"/>
  <updated>{now}</updated>
{chr(10).join(entries)}
</feed>
"""


def build(services: Path, data_dir: Path, out: Path, today: date | None = None) -> None:
    config = tomllib.loads(services.read_text())
    current = json.loads((data_dir / "current.json").read_text()) if (data_dir / "current.json").exists() else {}
    daily = json.loads((data_dir / "daily.json").read_text()) if (data_dir / "daily.json").exists() else {}
    events = json.loads((data_dir / "events.json").read_text()) if (data_dir / "events.json").exists() else []
    names = {c["id"]: {"de": c["name"], "en": c.get("name_en", c["name"])} for c in config["component"]}
    incidents = load_incidents(ROOT / "incidents") + detected(events, names)
    today = today or datetime.now(timezone.utc).date()

    if out.exists():
        shutil.rmtree(out)
    (out / "en").mkdir(parents=True)
    for asset in (ROOT / "site").iterdir():
        shutil.copy(asset, out / asset.name)
    (out / "index.html").write_text(render_page(config, current, daily, incidents, "de", today, ""))
    (out / "en" / "index.html").write_text(render_page(config, current, daily, incidents, "en", today, "../"))
    (out / "feed.xml").write_text(render_feed(config, incidents))
    summary = {
        "checked_at": current.get("checked_at"),
        "status": overall(current, incidents) if current else "unknown",
        "components": {
            c["id"]: {"status": current.get("components", {}).get(c["id"], {}).get("status", "unknown"),
                      "uptime_90d": uptime(daily.get(c["id"], {})), "stage": c.get("stage", "production")}
            for c in config["component"]
        },
        "active_incidents": [i.key for i in incidents if i.active],
    }
    (out / "status.json").write_text(json.dumps(summary, indent=1) + "\n")
    (out / ".nojekyll").write_text("")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--services", default="services.toml")
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", default="_site")
    args = parser.parse_args(argv)
    build(Path(args.services), Path(args.data), Path(args.out))
    print(f"built {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
