"""The guards on the public booking form — services/booking_guard.py.

No database and no network: the form token is a signature over a timestamp and
the honeypot is a string, so what's under test is exactly what a script meets
when it posts at ``/book/<slug>/book`` without having read the page.
"""
import unittest
from datetime import timedelta

from flask import Flask
from itsdangerous import TimestampSigner

from application.services.booking import BookingError
from application.services.booking_guard import (
    check_form_token, check_honeypot, issue_form_token,
)


class AgedSigner(TimestampSigner):
    """Signs as if it had happened ``old`` seconds ago."""

    old = 0

    def get_timestamp(self) -> int:
        return super().get_timestamp() - self.old


def app_for(**config) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config.setdefault("BOOKING_MIN_FORM_SECONDS", 3)
    app.config.setdefault("BOOKING_FORM_TTL_HOURS", 12)
    app.config.update(config)
    return app


class TicketTests(unittest.TestCase):
    def test_a_token_from_the_page_passes_once_it_has_aged(self):
        app = app_for(BOOKING_MIN_FORM_SECONDS=0)
        with app.app_context():
            check_form_token("alice", issue_form_token("alice"))  # no raise

    def test_no_token_is_refused(self):
        with app_for().app_context():
            with self.assertRaises(BookingError):
                check_form_token("alice", "")

    def test_a_forged_token_is_refused(self):
        with app_for(BOOKING_MIN_FORM_SECONDS=0).app_context():
            token = issue_form_token("alice")
            with self.assertRaises(BookingError):
                check_form_token("alice", token[:-2] + "xy")

    def test_a_token_minted_on_another_page_is_refused(self):
        """A scraped form can't be replayed against everybody's calendar."""
        with app_for(BOOKING_MIN_FORM_SECONDS=0).app_context():
            with self.assertRaises(BookingError):
                check_form_token("bob", issue_form_token("alice"))

    def test_a_form_that_comes_straight_back_is_refused(self):
        with app_for(BOOKING_MIN_FORM_SECONDS=3).app_context():
            with self.assertRaises(BookingError):
                check_form_token("alice", issue_form_token("alice"))

    def test_a_token_older_than_the_window_is_refused(self):
        app = app_for(BOOKING_MIN_FORM_SECONDS=0, BOOKING_FORM_TTL_HOURS=12)
        signer = AgedSigner("test-secret", salt="cairn-booking-form")
        signer.old = int(timedelta(hours=13).total_seconds())
        with app.app_context():
            with self.assertRaises(BookingError):
                check_form_token("alice", signer.sign("alice").decode("ascii"))


class HoneypotTests(unittest.TestCase):
    def test_the_field_name_is_one_no_browser_will_fill(self):
        """It was ``company`` once, and autofilling the name and email two
        rows above put a company in it — which refused a real booking."""
        from application.services.booking_guard import HONEYPOT_FIELD
        autofilled = ("name", "email", "company", "organization", "org",
                      "address", "street", "city", "zip", "postal", "phone",
                      "tel", "country", "url", "website", "username", "user",
                      "nickname", "title", "given", "family", "surname")
        self.assertNotIn(HONEYPOT_FIELD.lower(), autofilled)
        for word in autofilled:
            self.assertNotIn(word, HONEYPOT_FIELD.lower(),
                             f"{HONEYPOT_FIELD!r} reads like {word!r} to a browser")

    def test_an_empty_honeypot_passes(self):
        with app_for().app_context():
            check_honeypot("")
            check_honeypot(None)
            check_honeypot("   ")

    def test_a_filled_honeypot_is_refused(self):
        with app_for().app_context():
            with self.assertRaises(BookingError):
                check_honeypot("Acme Ltd")


if __name__ == "__main__":
    unittest.main()
