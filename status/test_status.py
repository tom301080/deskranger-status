"""Tests for probe.py and build.py (stdlib unittest; no network)."""
import json
import tempfile
import tomllib
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import build
import probe

ROOT = Path(__file__).resolve().parent.parent
CONFIG = tomllib.loads((ROOT / "services.toml").read_text())


class Evaluate(unittest.TestCase):
    def test_expected_answer_is_up(self):
        check = {"expect_status": [200], "expect_text": "ok"}
        self.assertEqual(probe.evaluate(check, 200, "ok", 120, 3000)["status"], "up")

    def test_slow_answer_is_degraded(self):
        self.assertEqual(probe.evaluate({}, 200, "", 4000, 3000)["status"], "degraded")

    def test_wrong_status_text_or_silence_is_down(self):
        self.assertEqual(probe.evaluate({"expect_status": [301]}, 200, "", 50, 3000)["status"], "down")
        self.assertEqual(probe.evaluate({"expect_text": "\"ok\":true"}, 200, "{}", 50, 3000)["status"], "down")
        self.assertEqual(probe.evaluate({}, 0, "URLError: timed out", 10000, 3000)["status"], "down")

    def test_one_failure_is_retried(self):
        answers = iter([(0, "timeout", 10000), (200, "ok", 90)])
        pauses = []
        result = probe.run_check({"url": "x", "expect_text": "ok"}, 3000, lambda url: next(answers), pauses.append)
        self.assertEqual(result["status"], "up")
        self.assertEqual(pauses, [probe.RETRY_PAUSE_S])


def round_of(status):
    return {c["id"]: {k["id"]: {"status": status, "code": 200, "ms": 100} for k in c["check"]} for c in CONFIG["component"]}


class Update(unittest.TestCase):
    def test_counts_days_records_changes_and_deploys_hourly(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            t0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
            first = probe.update(CONFIG, data, t0, round_of("up"))
            self.assertTrue(first["deploy"])
            self.assertFalse(first["changed"])
            second = probe.update(CONFIG, data, t0 + timedelta(minutes=5), round_of("up"))
            self.assertEqual(second, {"changed": False, "deploy": False})
            down = round_of("up")
            down["coord"]["meta"]["status"] = "down"
            third = probe.update(CONFIG, data, t0 + timedelta(minutes=10), down)
            self.assertEqual(third, {"changed": True, "deploy": True})
            daily = json.loads((data / "daily.json").read_text())
            self.assertEqual(daily["coord"]["2026-10-02"], {"n": 3, "up": 2, "degraded": 0, "ms": 300})
            events = json.loads((data / "events.json").read_text())
            self.assertEqual(events[0]["component"], "coord")
            self.assertEqual((events[0]["from"], events[0]["to"]), ("up", "down"))
            current = json.loads((data / "current.json").read_text())
            self.assertEqual(current["status"], "down")

    def test_old_days_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "daily.json").write_text(json.dumps({"website": {"2025-01-01": {"n": 1, "up": 1, "degraded": 0, "ms": 1}}}))
            probe.update(CONFIG, data, datetime(2026, 10, 2, tzinfo=timezone.utc), round_of("up"))
            self.assertNotIn("2025-01-01", json.loads((data / "daily.json").read_text())["website"])


class Build(unittest.TestCase):
    def test_detected_outage_has_start_end_and_name(self):
        events = [{"component": "coord", "from": "up", "to": "down", "at": "2026-10-02T12:10:00Z"},
                  {"component": "coord", "from": "down", "to": "up", "at": "2026-10-02T12:25:00Z"}]
        names = {"coord": {"de": "Verbindungsaufbau", "en": "Connection service"}}
        [incident] = build.detected(events, names)
        self.assertTrue(incident.auto)
        self.assertEqual(incident.title["de"], "Verbindungsaufbau nicht erreichbar")
        self.assertEqual((incident.resolved - incident.started).total_seconds(), 900)

    def test_an_outage_still_running_is_active(self):
        events = [{"component": "relay-eu", "from": "up", "to": "down", "at": "2026-10-02T12:10:00Z"}]
        [incident] = build.detected(events, {"relay-eu": {"de": "Relay Europa", "en": "Relay Europe"}})
        self.assertTrue(incident.active)

    def test_uptime_and_percent(self):
        self.assertIsNone(build.uptime({}))
        self.assertAlmostEqual(build.uptime({"d": {"n": 288, "up": 287, "degraded": 0, "ms": 0}}), 287 / 288)
        self.assertEqual(build.percent(1.0, "de"), "100 %")
        self.assertEqual(build.percent(0.9965, "de"), "99,65 %")
        self.assertEqual(build.percent(0.9965, "en"), "99.65 %")

    def test_site_renders_both_languages_without_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            build.build(ROOT / "services.toml", Path(tmp) / "nodata", out, today=date(2026, 10, 2))
            de = (out / "index.html").read_text()
            en = (out / "en" / "index.html").read_text()
            self.assertIn("Noch keine Daten", de)
            self.assertIn("No data yet", en)
            for component in CONFIG["component"]:
                self.assertIn(f'data-component="{component["id"]}"', de)
            self.assertEqual(de.count("<i class=\"none\""), CONFIG["site"]["days"] * len(CONFIG["component"]))
            self.assertTrue((out / "feed.xml").exists() and (out / "status.json").exists() and (out / "style.css").exists())

    def test_site_shows_status_and_uptime(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            probe.update(CONFIG, data, datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc), round_of("up"))
            out = Path(tmp) / "site"
            build.build(ROOT / "services.toml", data, out, today=date(2026, 10, 2))
            de = (out / "index.html").read_text()
            self.assertIn("Alle Dienste laufen", de)
            self.assertIn("Verfügbarkeit 100 %", de)
            self.assertIn("Testbetrieb", de)
            summary = json.loads((out / "status.json").read_text())
            self.assertEqual(summary["components"]["coord"]["stage"], "test")
            self.assertEqual(summary["components"]["website"]["uptime_90d"], 1.0)

    def test_incident_files_parse(self):
        for path in (ROOT / "incidents").glob("*.toml"):
            raw = tomllib.loads(path.read_text())
            self.assertIn("title", raw, path.name)
            known = {c["id"] for c in CONFIG["component"]}
            self.assertTrue(set(raw.get("components", [])) <= known, path.name)
            for update in raw.get("update", []):
                self.assertIn(update["status"], {"investigating", "identified", "monitoring", "resolved", "scheduled"}, path.name)


if __name__ == "__main__":
    unittest.main()
