"""Impressum and Datenschutz never name an operator Cairn wasn't told about.

Run with ``python -m unittest discover tests``.

Why this exists: these two pages used to carry one particular sole
proprietorship's postal address, phone number and VAT id in the template
source. Every self-hoster who started Cairn served that identity as *their*
Diensteanbieter. The identity now comes from ``BKTODO_LEGAL_*`` only, and the
case that must never regress is the UNCONFIGURED one: an instance nobody has
configured has to say so, and must not render a name, an address, a phone
number or a mail address from anywhere.

The bodies are rendered through a plain Jinja environment on purpose — they
use nothing from Flask, so this runs without an app, a database or Pillow.
"""
import os
import re
import unittest

import jinja2

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "application", "templates",
)

# The identity that used to be baked into the templates. If any of it ever
# comes back as literal template text, these tests are the alarm.
FORMER_OPERATOR = (
    "Bastian Kuhn", "Beratung und Entwicklung", "Streitfeldstr",
    "81673", "01607507827", "DE319059455", "bastian-kuhn.de",
    "Markel", "Hetzner",
)

CONFIGURED = {
    "LEGAL_OPERATOR_NAME": "Erika Mustermann",
    "LEGAL_OPERATOR_ADDITION": "Musterbetrieb",
    "LEGAL_OPERATOR_ADDRESS": "Musterstr. 1|12345 Musterstadt",
    "LEGAL_OPERATOR_EMAIL": "kontakt@example.org",
    "LEGAL_OPERATOR_PHONE": "+49 30 000000",
}


def _env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        autoescape=True,
    )


def _render(name, config):
    from application.services.legal import operator
    tpl = _env().get_template("legal/" + name)
    return tpl.render(operator=operator(config))


class OperatorConfigTests(unittest.TestCase):
    """``services.legal.operator`` — what counts as configured."""

    def test_empty_config_is_not_configured(self):
        from application.services.legal import operator
        op = operator({})
        self.assertFalse(op["configured"])
        self.assertEqual(op["missing"], [
            "BKTODO_LEGAL_OPERATOR_NAME",
            "BKTODO_LEGAL_OPERATOR_ADDRESS",
            "BKTODO_LEGAL_OPERATOR_EMAIL",
        ])

    def test_half_filled_is_still_not_configured(self):
        """A name without a way to reach anybody is not an Impressum."""
        from application.services.legal import operator
        op = operator({"LEGAL_OPERATOR_NAME": "Erika Mustermann"})
        self.assertFalse(op["configured"])
        self.assertEqual(op["missing"], [
            "BKTODO_LEGAL_OPERATOR_ADDRESS",
            "BKTODO_LEGAL_OPERATOR_EMAIL",
        ])

    def test_whitespace_only_counts_as_unset(self):
        from application.services.legal import operator
        cfg = dict(CONFIGURED, LEGAL_OPERATOR_EMAIL="   ")
        self.assertFalse(operator(cfg)["configured"])

    def test_multiline_values_split_on_pipe_and_newline(self):
        from application.services.legal import operator
        op = operator({
            "LEGAL_OPERATOR_ADDRESS": " Musterstr. 1 | 12345 Musterstadt ",
            "LEGAL_OPERATOR_INSURANCE": "Versicherung AG\nMusterstadt\r\nweltweit",
        })
        self.assertEqual(op["address"], ["Musterstr. 1", "12345 Musterstadt"])
        self.assertEqual(op["insurance"],
                         ["Versicherung AG", "Musterstadt", "weltweit"])

    def test_full_config_is_configured(self):
        from application.services.legal import operator
        op = operator(CONFIGURED)
        self.assertTrue(op["configured"])
        self.assertEqual(op["missing"], [])


class UnconfiguredInstanceTests(unittest.TestCase):
    """The case that must never leak somebody else's legal identity."""

    def test_impressum_names_nobody(self):
        out = _render("_impressum_body.html", {})
        self.assertIn("keine Impressumsangaben hinterlegt", out)
        self.assertNotIn("mailto:", out)
        for token in FORMER_OPERATOR:
            self.assertNotIn(token, out)

    def test_impressum_tells_the_admin_what_to_set(self):
        out = _render("_impressum_body.html", {})
        self.assertIn("BKTODO_LEGAL_OPERATOR_NAME", out)
        self.assertIn("BKTODO_LEGAL_OPERATOR_EMAIL", out)

    def test_impressum_drops_the_diensteanbieter_boilerplate(self):
        """Without a known operator, "wir als Diensteanbieter" has no subject."""
        out = _render("_impressum_body.html", {})
        self.assertNotIn("Haftung für Inhalte", out)
        self.assertNotIn("Umsatzsteuer", out)

    def test_datenschutz_names_nobody(self):
        out = _render("_datenschutz_body.html", {})
        self.assertIn("keine Impressumsangaben hinterlegt", out)
        self.assertNotIn("mailto:", out)
        for token in FORMER_OPERATOR:
            self.assertNotIn(token, out)

    def test_datenschutz_keeps_what_the_software_does(self):
        """The substance is about Cairn, not about who runs it — it stays."""
        out = _render("_datenschutz_body.html", {})
        for heading in ("Server-Logfiles", "Konto", "Deine Inhalte", "Cookies",
                        "Optionale Integrationen", "Deine Rechte"):
            self.assertIn(heading, out)
        # …and the logfile paragraph survives without a hosting provider.
        self.assertIn("Server-Logfiles erzeugt", out)
        self.assertNotIn("gehostet", out)

    def test_half_configured_instance_shows_the_notice_not_a_fragment(self):
        out = _render("_impressum_body.html",
                      {"LEGAL_OPERATOR_NAME": "Erika Mustermann"})
        self.assertIn("keine Impressumsangaben hinterlegt", out)
        self.assertNotIn("Angaben gemäß § 5 DDG", out)


class ConfiguredInstanceTests(unittest.TestCase):

    def test_impressum_prints_the_configured_identity(self):
        out = _render("_impressum_body.html", CONFIGURED)
        self.assertNotIn("keine Impressumsangaben hinterlegt", out)
        for value in ("Erika Mustermann", "Musterbetrieb", "Musterstr. 1",
                      "12345 Musterstadt", "kontakt@example.org",
                      "+49 30 000000"):
            self.assertIn(value, out)
        self.assertIn("Haftung für Inhalte", out)

    def test_optional_sections_are_left_out_when_unset(self):
        out = _render("_impressum_body.html", CONFIGURED)
        for heading in ("Umsatzsteuer-ID", "Berufshaftpflichtversicherung",
                        "Registereintrag", "Vertreten durch"):
            self.assertNotIn(heading, out)

    def test_optional_sections_appear_when_set(self):
        cfg = dict(
            CONFIGURED,
            LEGAL_OPERATOR_VAT_ID="DE000000000",
            LEGAL_OPERATOR_REGISTER="Amtsgericht Musterstadt, HRB 1",
            LEGAL_OPERATOR_REPRESENTATIVE="Erika Mustermann",
            LEGAL_OPERATOR_INSURANCE="Versicherung AG|Musterstadt",
        )
        out = _render("_impressum_body.html", cfg)
        for value in ("DE000000000", "Amtsgericht Musterstadt, HRB 1",
                      "Versicherung AG", "Registereintrag", "Vertreten durch"):
            self.assertIn(value, out)

    def test_datenschutz_prints_the_responsible_party(self):
        out = _render("_datenschutz_body.html", CONFIGURED)
        self.assertNotIn("keine Impressumsangaben hinterlegt", out)
        self.assertIn("Erika Mustermann — Musterbetrieb", out)
        self.assertIn("Musterstr. 1, 12345 Musterstadt", out)
        self.assertIn("kontakt@example.org", out)

    def test_hosting_provider_is_named_only_when_configured(self):
        cfg = dict(CONFIGURED,
                   LEGAL_HOSTING_PROVIDER="der Example Hosting GmbH (Musterstadt)")
        out = _render("_datenschutz_body.html", cfg)
        self.assertIn("Cairn wird bei der Example Hosting GmbH (Musterstadt) "
                      "gehostet.", out)


class ShippedSourceTests(unittest.TestCase):
    """No operator identity in the files the public repo ships."""

    FILES = (
        "legal/_impressum_body.html",
        "legal/_datenschutz_body.html",
        "legal/_not_configured.html",
        "legal/impressum.html",
        "legal/datenschutz.html",
        "about.html",
    )

    def test_no_hardcoded_identity(self):
        for name in self.FILES:
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
                src = fh.read()
            for token in FORMER_OPERATOR:
                self.assertNotIn(token, src, f"{name} still names {token!r}")

    def test_no_hardcoded_mail_address(self):
        """Any address on these pages has to come through a template variable."""
        pattern = re.compile(r"mailto:[A-Za-z0-9._%+-]+@")
        for name in self.FILES:
            with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertIsNone(pattern.search(src),
                              f"{name} carries a literal mail address")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
