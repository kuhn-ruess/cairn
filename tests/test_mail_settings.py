"""How the SMTP settings resolve — in particular which kind of TLS.

Run with ``python -m unittest discover tests``. No database and no network:
only ``smtp_settings`` is asked, which is the one place the decision is made.

Worth a test because the failure mode is silent: pointing a plain ``SMTP``
connection at an implicit-TLS port doesn't produce an error, it produces a
hang until the timeout, and "no mail arrives" is all the operator sees.
"""
import unittest

from application import create_app
from application.services.mail import smtp_settings


class SmtpSettingsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app("application.config.TestConfig")

    def settings(self, **over):
        with self.app.app_context():
            self.app.config.update(SMTP_HOST="mail.example", SMTP_PORT="587",
                                   SMTP_USER="", SMTP_PASSWORD="", SMTP_FROM="",
                                   SMTP_TLS="1", SMTP_SSL="")
            self.app.config.update(**over)
            return smtp_settings()

    def test_587_is_starttls(self):
        got = self.settings()
        self.assertFalse(got["ssl"])
        self.assertTrue(got["starttls"])

    def test_465_means_implicit_tls_without_being_told(self):
        """A deployment that sets only host, port and login should work."""
        self.assertTrue(self.settings(SMTP_PORT="465")["ssl"])

    def test_implicit_tls_can_be_asked_for_on_any_port(self):
        self.assertTrue(self.settings(SMTP_PORT="2525", SMTP_SSL="1")["ssl"])

    def test_it_can_also_be_refused_on_465(self):
        """Some tunnels really do want plain on that port — saying "0" is
        taken at its word rather than overruled by the port."""
        self.assertFalse(self.settings(SMTP_PORT="465", SMTP_SSL="0")["ssl"])

    def test_encryption_can_be_switched_off_entirely(self):
        got = self.settings(SMTP_TLS="0")
        self.assertFalse(got["ssl"])
        self.assertFalse(got["starttls"])

    def test_the_sender_falls_back_to_the_login(self):
        self.assertEqual(self.settings(SMTP_USER="me@example.com")["sender"],
                         "me@example.com")
        self.assertEqual(self.settings(SMTP_USER="me@example.com",
                                       SMTP_FROM="no-reply@example.com")["sender"],
                         "no-reply@example.com")


if __name__ == "__main__":
    unittest.main()
