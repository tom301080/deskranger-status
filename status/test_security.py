"""Tests of the security checks page (DeskRanger ADR 0046): what is published, and when."""
import unittest
from datetime import datetime, timezone

import security

T1, T2 = "2026-10-05T04:10:00Z", "2026-10-12T04:10:00Z"


class Merge(unittest.TestCase):
    def test_a_failed_check_keeps_its_last_pass_and_publishes_no_detail(self):
        first = security.merge({}, {"tls": (True, "A+")}, T1)
        second = security.merge(first, {"tls": (False, "B")}, T2)
        tls = second["checks"]["tls"]
        self.assertFalse(tls["passed"])
        self.assertEqual((tls["last_passed"], tls["last_detail"], tls["detail"]), (T1, "A+", ""))


class SecurityTxt(unittest.TestCase):
    NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)

    def test_contact_and_a_far_expiry_pass(self):
        text = "# x\nContact: mailto:a@b.c\nExpires: 2027-09-30T00:00:00.000Z\n"
        self.assertTrue(security.security_txt_ok(text, self.NOW))

    def test_expiring_soon_or_no_contact_fails(self):
        self.assertFalse(security.security_txt_ok("Contact: mailto:a@b.c\nExpires: 2026-10-20T00:00:00Z\n", self.NOW))
        self.assertFalse(security.security_txt_ok("Expires: 2027-09-30T00:00:00Z\n", self.NOW))


class Render(unittest.TestCase):
    def test_only_checks_that_passed_once_appear(self):
        web = security.merge({}, {"observatory": (True, "A+ (115/100)"), "tls": (False, "")}, T1)
        page = security.render(web, {}, "de", "../", "../en/security/")
        self.assertIn("Sicherheits-Header der Website", page)
        self.assertIn("A+ (115/100)", page)
        self.assertNotIn("Verschlüsselung der Website", page)

    def test_a_failing_check_shows_only_when_it_last_passed(self):
        web = security.merge(security.merge({}, {"tls": (True, "A+")}, T1), {"tls": (False, "B")}, T2)
        page = security.render(web, {}, "en", "../../", "../../security/")
        self.assertIn("Last passed 2026-10-05", page)
        self.assertNotIn(">B<", page)
        self.assertNotIn("B)", page)

    def test_code_checks_come_from_the_private_summary(self):
        code = {"checks": {"secrets": {"passed": True, "checked": T1, "last_passed": T1}}}
        page = security.render({}, code, "de", "../", "../en/security/")
        self.assertIn("Keine Zugangsdaten im Code", page)
        self.assertIn("geprüft am 05.10.2026", page)


if __name__ == "__main__":
    unittest.main()
