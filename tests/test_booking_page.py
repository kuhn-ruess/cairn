"""The booking page end to end: settings form, public page, confirmation.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one. No network: the
calendar layer is replaced, so what's under test is the bookkeeping around it
— who may see the page, what the form refuses to save, and the three ways two
visitors can end up fighting over one slot.
"""
import io
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

BERLIN = "Europe/Berlin"


def png(width: int, height: int) -> bytes:
    """A real PNG — the upload path sniffs the bytes, so a fake won't do."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (80, 120, 160)).save(buf, format="PNG")
    return buf.getvalue()


def quartered_png(side: int) -> bytes:
    """A square split into four flat colours, so a crop can be told apart by
    the colour that comes back."""
    from PIL import Image
    img = Image.new("RGB", (side, side), (255, 0, 0))          # top-left red
    half = side // 2
    img.paste(Image.new("RGB", (half, half), (0, 255, 0)), (half, 0))    # green
    img.paste(Image.new("RGB", (half, half), (0, 0, 255)), (0, half))    # blue
    img.paste(Image.new("RGB", (half, half), (255, 255, 0)), (half, half))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def middle_pixel(data: bytes):
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return img.getpixel((img.width // 2, img.height // 2))


def looks_like(pixel, expected, tol=16) -> bool:
    """WebP is lossy, so a flat green comes back as very nearly green."""
    return all(abs(a - b) <= tol for a, b in zip(pixel, expected))


def form_data(**over):
    data = {
        "enabled": "y", "slug": "alice", "title": "Talk to me", "intro": "Hi.",
        "durations": "30, 60", "days_ahead": "14",
        "write_calendar_id": "", "busy_calendar_ids": [],
        "min_notice_hours": "0", "buffer_minutes": "0", "max_per_day": "0",
        "listed": "y",
    }
    for day in range(5):
        data[f"day-{day}"] = "y"
        data[f"start-{day}"] = "09:00"
        data[f"end-{day}"] = "17:00"
    data.update(over)
    return data


def event(start, end, title="Busy", calendar="Work", all_day=False):
    """A calendar event as the real fetch would hand it over."""
    from application.services.calendar_sync import CalendarEvent
    return CalendarEvent(uid=f"u-{start:%Y%m%d%H%M}", title=title, start=start,
                         end=end, all_day=all_day, calendar_name=calendar,
                         calendar_color="#d79921")


@unittest.skipIf(connect is None, "mongoengine not installed")
class BookingPageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            from application.models.booking import Booking, BookingPhoto
            from application.models.user import User
            for name in ("book-test", "book-other"):
                for u in User.objects(username=name):
                    Booking.objects(owner=u).delete()
                    BookingPhoto.objects(owner=u).delete()
                    u.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.booking import Booking, BookingPhoto
        from application.models.calendar import ExternalCalendar
        from application.models.user import User
        from application.services import booking as svc
        self.User = User
        self.Booking = Booking
        self.svc = svc
        with self.app.app_context():
            for name in ("book-test", "book-other"):
                for u in User.objects(username=name):
                    Booking.objects(owner=u).delete()
                    BookingPhoto.objects(owner=u).delete()
                    u.delete()
            user = User(username="book-test", email="b@example.com",
                        level="premium", timezone=BERLIN)
            user.set_password("x")
            self.cal = ExternalCalendar(provider="caldav", name="Work",
                                        can_write=True,
                                        caldav_url="https://dav.example/w/")
            user.calendars = [self.cal]
            user.save()
            self.user = user
            self.cal_id = str(self.cal.pk_ref)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

        # No calendar is ever reached: busy times and event writes are stubbed.
        # ``busy_events`` is the one that would talk to a calendar and
        # ``busy_intervals`` its projection, so the fake sits at the same
        # seam — and self.client is logged in AS THE HOST, which is the path
        # that reads the events rather than the intervals.
        self._busy = svc.busy_intervals
        self._events = svc.busy_events
        self._write = svc._write_event
        self.busy = []
        self.events = None        # None = derive plain ones from self.busy
        self.written = []

        def fake_events(user, page, since, until):
            if self.events is not None:
                return list(self.events)
            return [event(start, end) for start, end in self.busy]

        svc.busy_events = fake_events
        svc.busy_intervals = lambda user, page, since, until: [
            (e.start, e.end) for e in fake_events(user, page, since, until)]

        def fake_write(user, page, booking):
            self.written.append(booking)
            booking.event_id = "evt-1"
            booking.calendar_id = page.write_calendar_id
            booking.save()

        svc._write_event = fake_write

        # The real ``_write_event`` asks the calendar for the host's address;
        # by default it names none and the Cairn login address stands in.
        from application.services import calendar_writeback
        self.organizer = ""
        real_organizer = calendar_writeback.organizer_address
        calendar_writeback.organizer_address = lambda cal, user: self.organizer
        self.addCleanup(setattr, calendar_writeback, "organizer_address",
                        real_organizer)

    def tearDown(self):
        self.svc.busy_intervals = self._busy
        self.svc.busy_events = self._events
        self.svc._write_event = self._write

    # -- helpers ----------------------------------------------------------
    def save_page(self, **over):
        over.setdefault("write_calendar_id", self.cal_id)
        return self.client.post("/settings/booking", data=form_data(**over),
                                content_type="multipart/form-data",
                                follow_redirects=True)

    def reload(self):
        with self.app.app_context():
            return self.User.objects.get(username="book-test")

    def first_slot(self, duration=30):
        resp = self.client.get(f"/book/alice/slots?duration={duration}")
        return resp.get_json()["slots"][0]

    def form_token(self, slug="alice"):
        """What the rendered form carries (services/booking_guard.py)."""
        from application.services.booking_guard import issue_form_token
        with self.app.app_context():
            return issue_form_token(slug)

    def book(self, start, duration=30, **over):
        data = {"start": start, "duration": str(duration), "name": "Bob",
                "email": "bob@example.com", "note": "Hallo", "tz": "America/New_York",
                "form_token": self.form_token()}
        data.update(over)
        return self.client.post("/book/alice/book", data=data)

    # -- the settings form ------------------------------------------------
    def test_saving_the_form_configures_the_page(self):
        self.save_page()
        page = self.reload().booking
        self.assertTrue(page.enabled)
        self.assertEqual(page.slug, "alice")
        self.assertEqual(page.ordered_durations(), [30, 60])
        self.assertEqual(len(page.hours), 5)
        self.assertEqual(page.write_calendar_id, self.cal_id)

    def test_a_live_page_without_a_write_calendar_is_allowed_and_warns(self):
        """Requiring one meant a user with no writable calendar could not
        switch the page on at all, and their link just kept saying 404."""
        resp = self.save_page(write_calendar_id="")
        self.assertTrue(self.reload().booking.enabled)
        self.assertIn(b"recorded in\n    Cairn only", resp.data.replace(b"\r", b""))
        self.assertEqual(self.client.get("/book/alice").status_code, 200)

    def test_booking_works_without_a_calendar_to_write_to(self):
        self.save_page(write_calendar_id="")
        self.svc._write_event = self._write        # the real one
        start = self.first_slot()
        self.assertEqual(self.book(start).status_code, 200)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertIn("No calendar", booking.event_error)

    def test_an_end_before_its_start_is_refused_and_saves_nothing(self):
        self.save_page()
        resp = self.save_page(**{"end-1": "08:00", "title": "Changed"})
        self.assertIn(b"end has to be after the start", resp.data)
        self.assertEqual(self.reload().booking.title, "Talk to me")

    def test_a_slug_someone_else_uses_is_refused(self):
        self.save_page()
        with self.app.app_context():
            other = self.User(username="book-other", email="o@example.com",
                              level="premium")
            other.set_password("x")
            other.booking.slug = "taken"
            other.save()
        resp = self.save_page(slug="taken")
        self.assertIn(b"already uses that link name", resp.data)
        self.assertEqual(self.reload().booking.slug, "alice")

    def test_a_slug_with_spaces_is_refused(self):
        resp = self.save_page(slug="my page")
        self.assertIn(b"lowercase letters, digits and dashes", resp.data)

    # -- the public page --------------------------------------------------
    def test_the_page_404s_until_it_is_switched_on(self):
        self.save_page(enabled="")
        self.assertEqual(self.client.get("/book/alice").status_code, 404)
        self.save_page()
        self.assertEqual(self.client.get("/book/alice").status_code, 200)

    def test_an_unknown_slug_is_a_404(self):
        self.assertEqual(self.client.get("/book/nobody").status_code, 404)

    def test_slots_are_utc_instants_and_respect_busy_times(self):
        self.save_page()
        data = self.client.get("/book/alice/slots?duration=60").get_json()
        self.assertTrue(data["slots"])
        self.assertTrue(all(s.endswith("+00:00") for s in data["slots"]))
        taken = datetime.fromisoformat(data["slots"][0])
        self.busy = [(taken, taken + timedelta(minutes=60))]
        again = self.client.get("/book/alice/slots?duration=60").get_json()
        self.assertNotIn(data["slots"][0], again["slots"])

    def test_a_length_that_is_not_offered_is_rejected(self):
        self.save_page()
        self.assertEqual(self.client.get("/book/alice/slots?duration=45").status_code,
                         400)

    def test_an_unreadable_calendar_stops_the_page_rather_than_guessing(self):
        self.save_page()

        def broken(user, page, since, until):
            raise self.svc.BookingError("The calendar “Work” could not be read")

        self.svc.busy_intervals = broken
        self.svc.busy_events = broken
        resp = self.client.get("/book/alice/slots?duration=30")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("could not be read", resp.get_json()["error"])
        # …and for a stranger just the same: the page refuses to guess for
        # anybody.
        stranger = self.app.test_client()
        self.assertEqual(stranger.get("/book/alice/slots?duration=30").status_code,
                         503)

    # -- booking ----------------------------------------------------------
    def test_a_booking_is_stored_written_and_announced(self):
        self.save_page()
        start = self.first_slot()
        resp = self.book(start)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"You're booked", resp.data)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.name, "Bob")
            self.assertEqual(booking.duration_minutes, 30)
            self.assertEqual(booking.event_id, "evt-1")
            self.assertEqual(booking.guest_timezone, "America/New_York")
        self.assertEqual(len(self.written), 1)
        bell = [n for n in self.reload().notifications if n.kind == "booking"]
        self.assertEqual(len(bell), 1)
        self.assertIn("Bob", bell[0].text)

    def test_the_bell_points_at_the_appointments_not_at_the_settings_form(self):
        """A new booking is news about an appointment, so the entry has to
        land on the list that shows it. The settings page is where the
        booking page is CONFIGURED — it was the target only because the
        feature used to live there, and clicking the bell then answered a
        question nobody had asked."""
        self.save_page()
        self.book(self.first_slot())
        bell = [n for n in self.reload().notifications if n.kind == "booking"]
        self.assertEqual(len(bell), 1)
        with self.app.test_request_context():
            from flask import url_for
            self.assertEqual(bell[0].link, url_for("bookings.index"))
        self.assertNotIn("/settings/", bell[0].link)
        # And the link actually resolves for the host.
        self.assertEqual(self.client.get(bell[0].link).status_code, 200)

    def test_a_booked_slot_disappears_from_the_next_visitor(self):
        self.save_page()
        start = self.first_slot()
        self.book(start)
        self.assertNotIn(start,
                         self.client.get("/book/alice/slots?duration=30")
                         .get_json()["slots"])

    def test_the_same_slot_cannot_be_booked_twice(self):
        self.save_page()
        start = self.first_slot()
        self.book(start)
        # Somebody else: the same visitor again is a resubmit (see
        # test_sending_the_same_booking_twice_books_it_once).
        resp = self.book(start, name="Carol", email="carol@example.com")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"just taken", resp.data)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 1)

    def test_an_overlapping_slot_is_refused_even_at_a_different_start(self):
        """10:00–11:00 booked, then 10:30 for 30 minutes: different start, so
        the unique index says nothing — the availability check has to."""
        self.save_page()
        hour = self.first_slot(60)
        self.book(hour, 60)
        half = (datetime.fromisoformat(hour) + timedelta(minutes=30)).isoformat()
        resp = self.book(half, 30)
        self.assertEqual(resp.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 1)

    def test_a_time_outside_the_offered_hours_is_refused(self):
        self.save_page()
        start = datetime.fromisoformat(self.first_slot())
        midnight = start.replace(hour=2, minute=0)
        resp = self.book(midnight.isoformat())
        self.assertEqual(resp.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 0)

    def test_the_notice_window_is_enforced_on_the_post_too(self):
        """A stale page still shows this morning's slots; the POST must not
        take one."""
        self.save_page(min_notice_hours="0")
        start = self.first_slot()
        self.save_page(min_notice_hours="720")
        resp = self.book(start)
        self.assertEqual(resp.status_code, 400)

    # -- the list in the main menu ----------------------------------------
    def test_the_appointments_are_a_page_of_their_own(self):
        self.save_page()
        self.book(self.first_slot())
        body = self.client.get("/bookings/").get_data(as_text=True)
        self.assertIn("Bob", body)
        self.assertIn("/cancel", body)

    def test_the_menu_only_carries_it_once_the_page_is_on(self):
        """A booking page nobody switched on has no appointments to work
        with, so the entry — and the count query behind it — stays away."""
        before = self.client.get("/bookings/").get_data(as_text=True)
        self.assertNotIn('href="/bookings/"', before)
        self.save_page()
        after = self.client.get("/bookings/").get_data(as_text=True)
        self.assertIn('href="/bookings/"', after)

    # -- calling one off --------------------------------------------------
    def test_cancelling_gives_the_slot_back(self):
        self.save_page()
        start = self.first_slot()
        self.assertEqual(self.book(start).status_code, 200)
        self.assertNotIn(start, self.client.get(
            "/book/alice/slots?duration=30").get_json()["slots"])
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
        resp = self.client.post(f"/bookings/{booking.pk}/cancel",
                                data={"notify": ""}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 0)
        self.assertIn(start, self.client.get(
            "/book/alice/slots?duration=30").get_json()["slots"])

    def test_cancelling_takes_the_calendar_entry_with_it(self):
        from application.services import calendar_writeback
        self.save_page()
        self.book(self.first_slot())
        gone = []
        real = calendar_writeback.delete_event
        calendar_writeback.delete_event = lambda cal, user, eid: gone.append(eid)
        try:
            with self.app.app_context():
                booking = self.Booking.objects.get(owner=self.user)
            self.client.post(f"/bookings/{booking.pk}/cancel", data={})
        finally:
            calendar_writeback.delete_event = real
        self.assertEqual(gone, ["evt-1"])

    def test_somebody_elses_appointment_cannot_be_cancelled(self):
        """Owner-scoped by the query, so another host's booking is a 404."""
        self.save_page()
        self.book(self.first_slot())
        with self.app.app_context():
            other = self.User(username="book-other", email="o@example.com",
                              level="premium")
            other.set_password("x")
            other.save()
            booking = self.Booking.objects.get(owner=self.user)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(other.pk)
            sess["_fresh"] = True
        self.assertEqual(client.post(
            f"/bookings/{booking.pk}/cancel", data={}).status_code, 404)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 1)

    # -- the guards on the form (services/booking_guard.py) ---------------
    def test_the_rendered_page_hands_out_a_working_form_token(self):
        """What a browser actually does: read the form, post what's in it."""
        import re
        self.save_page()
        html = self.client.get("/book/alice").get_data(as_text=True)
        found = re.search(r'name="form_token" value="([^"]+)"', html)
        self.assertIsNotNone(found)
        resp = self.book(self.first_slot(), form_token=found.group(1))
        self.assertEqual(resp.status_code, 200)

    def test_a_post_that_never_loaded_the_page_books_nothing(self):
        """The shape a script takes: straight at the endpoint, no token."""
        self.save_page()
        resp = self.client.post("/book/alice/book", data={
            "start": self.first_slot(), "duration": "30", "name": "Bot",
            "email": "bot@example.com"})
        self.assertEqual(resp.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 0)

    def test_a_token_from_somebody_elses_page_books_nothing(self):
        self.save_page()
        resp = self.book(self.first_slot(), form_token=self.form_token("someone-else"))
        self.assertEqual(resp.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 0)

    def test_filling_the_honeypot_books_nothing(self):
        from application.services.booking_guard import HONEYPOT_FIELD
        self.save_page()
        resp = self.book(self.first_slot(), **{HONEYPOT_FIELD: "Acme Ltd"})
        self.assertEqual(resp.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 0)

    def test_the_page_ceiling_stops_a_flood_whatever_the_ip(self):
        """The limit that doesn't care where the request came from: once the
        page has taken its hour's worth, the next one is refused."""
        self.save_page()
        slots = self.client.get("/book/alice/slots?duration=30").get_json()["slots"]
        was = self.app.config["BOOKING_MAX_NEW_PER_HOUR"]
        self.app.config["BOOKING_MAX_NEW_PER_HOUR"] = 1
        try:
            self.assertEqual(self.book(slots[0]).status_code, 200)
            resp = self.book(slots[1], email="someone@example.com")
            self.assertEqual(resp.status_code, 400)
            self.assertIn(b"try again later", resp.data)
        finally:
            self.app.config["BOOKING_MAX_NEW_PER_HOUR"] = was
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 1)

    def test_one_address_cannot_book_over_and_over_in_a_day(self):
        """What stops the page filling a stranger's inbox with
        confirmations."""
        self.save_page()
        slots = self.client.get("/book/alice/slots?duration=30").get_json()["slots"]
        was = self.app.config["BOOKING_MAX_PER_EMAIL_PER_DAY"]
        self.app.config["BOOKING_MAX_PER_EMAIL_PER_DAY"] = 1
        try:
            self.assertEqual(self.book(slots[0]).status_code, 200)
            resp = self.book(slots[1])           # same email as the helper uses
            self.assertEqual(resp.status_code, 400)
            self.assertIn(b"booked several times today", resp.data)
            # …and somebody else still gets a slot.
            self.assertEqual(self.book(slots[1], email="eve@example.com")
                             .status_code, 200)
        finally:
            self.app.config["BOOKING_MAX_PER_EMAIL_PER_DAY"] = was

    def test_appointments_already_held_do_not_block_the_next_one(self):
        """The rule counts the RATE, not the stock: a client who books six
        sessions for the term must not be locked out of the seventh — which
        is what the first cut of this did."""
        self.save_page()
        slots = self.client.get("/book/alice/slots?duration=30").get_json()["slots"]
        was = self.app.config["BOOKING_MAX_PER_EMAIL_PER_DAY"]
        self.app.config["BOOKING_MAX_PER_EMAIL_PER_DAY"] = 2
        try:
            with self.app.app_context():
                # Three appointments this address booked last week, all still
                # ahead of it.
                then = datetime.now(timezone.utc) - timedelta(days=7)
                for n in range(3):
                    start = datetime.fromisoformat(slots[0]) + timedelta(days=30 + n)
                    self.Booking(owner=self.user, start=start,
                                 end=start + timedelta(minutes=30),
                                 duration_minutes=30, name="Bob",
                                 email="bob@example.com", created_at=then).save()
            self.assertEqual(self.book(slots[0]).status_code, 200)
        finally:
            self.app.config["BOOKING_MAX_PER_EMAIL_PER_DAY"] = was

    def test_a_booking_without_an_email_is_refused(self):
        self.save_page()
        resp = self.book(self.first_slot(), email="not-an-address")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"valid email", resp.data)

    def test_a_failed_event_write_still_keeps_the_slot(self):
        """The visitor was told yes; the owner is the one who learns it didn't
        reach the calendar."""
        self.save_page()
        self.svc._write_event = self._write   # the real one, with no calendar
        with self.app.app_context():
            user = self.User.objects.get(username="book-test")
            user.booking.write_calendar_id = ""
            user.save()
        start = self.first_slot()
        resp = self.book(start)
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertIn("No calendar", booking.event_error)
        self.assertNotIn(start,
                         self.client.get("/book/alice/slots?duration=30")
                         .get_json()["slots"])

    # -- what blocks the calendar (the owner's own view) -------------------
    def blocked(self):
        """Two appointments on the first bookable day, and the page saved."""
        self.save_page()
        first = datetime.fromisoformat(self.first_slot())
        self.events = [
            event(first, first + timedelta(minutes=60), "Team standup"),
            event(first + timedelta(hours=3), first + timedelta(hours=4),
                  "Kunde Müller", calendar="Privat"),
        ]
        return first

    def slots_json(self, client=None, query=""):
        return (client or self.client).get(
            "/book/alice/slots?duration=30" + query).get_json()

    def test_the_owner_is_told_what_blocks_the_day(self):
        self.blocked()
        busy = self.slots_json()["busy"]
        self.assertEqual([b["title"] for b in busy], ["Team standup", "Kunde Müller"])
        self.assertEqual([b["calendar"] for b in busy], ["Work", "Privat"])
        self.assertTrue(all(b["start"] and b["end"] for b in busy))

    def test_a_visitor_is_told_nothing_about_the_calendar(self):
        """The page is public. What is in the host's calendar is not."""
        self.blocked()
        stranger = self.app.test_client()
        answer = self.slots_json(stranger)
        self.assertNotIn("busy", answer)
        self.assertNotIn(b"Team standup",
                         stranger.get("/book/alice").data)

    def test_another_logged_in_user_is_not_the_owner(self):
        self.blocked()
        with self.app.app_context():
            other = self.User(username="book-other", email="o@example.com",
                              level="premium")
            other.set_password("x")
            other.save()
            other_id = str(other.pk)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = other_id
            sess["_fresh"] = True
        self.assertNotIn("busy", self.slots_json(client))

    def test_an_embed_hides_it_even_from_the_owner(self):
        """An embed renders inside a site Cairn doesn't control."""
        self.blocked()
        self.assertNotIn("busy", self.slots_json(query="&embed=1"))

    def test_the_page_tells_the_script_which_one_it_is(self):
        self.save_page()
        self.assertIn(b'data-owner="1"', self.client.get("/book/alice").data)
        self.assertIn(b'data-owner="0"',
                      self.app.test_client().get("/book/alice").data)
        self.assertIn(b'data-owner="0"',
                      self.client.get("/book/alice?embed=1").data)

    def test_the_owner_s_page_still_costs_one_round_of_calendar_reads(self):
        """The events are read once and the intervals derived — not fetched a
        second time for the slot maths."""
        self.blocked()          # this already asks once, for the first slot
        calls = []
        real = self.svc.busy_events
        self.svc.busy_events = lambda *a, **kw: calls.append(1) or real(*a, **kw)
        self.slots_json()
        self.assertEqual(len(calls), 1)

    # -- how you meet -----------------------------------------------------
    def connect_microsoft(self):
        """A connected Microsoft account, without going near Entra."""
        self.app.config["MS_CLIENT_ID"] = "cid"
        self.app.config["MS_CLIENT_SECRET"] = "secret"
        self.addCleanup(self.app.config.update, MS_CLIENT_ID="", MS_CLIENT_SECRET="")
        with self.app.app_context():
            user = self.User.objects.get(username="book-test")
            user.ms_refresh_token = "stored"
            user.ms_account = "alice@contoso.com"
            user.save()

    def stub_teams(self, answer=None, error=None):
        from application.services import msgraph

        def fake(user, *, subject, start, end):
            if error:
                raise msgraph.GraphError(error)
            return {"join_url": answer, "id": "meet-1"}

        real = msgraph.create_meeting
        msgraph.create_meeting = fake
        self.addCleanup(setattr, msgraph, "create_meeting", real)

    def test_a_fixed_link_is_handed_to_every_booking(self):
        self.save_page(meeting_mode="link",
                       meeting_link="https://meet.example/alice")
        resp = self.book(self.first_slot())
        self.assertIn(b"Join link", resp.data)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.meeting_url, "https://meet.example/alice")
            self.assertEqual(booking.meeting_provider, "link")

    def test_that_link_has_to_be_there_and_has_to_be_a_link(self):
        resp = self.save_page(meeting_mode="link", meeting_link="")
        self.assertIn(b"Give the meeting link", resp.data)
        resp = self.save_page(meeting_mode="link", meeting_link="meet.example")
        self.assertIn(b"http:// or https://", resp.data)
        self.assertEqual(self.reload().booking.meeting_mode, "none")

    def test_teams_cannot_be_promised_without_a_connected_account(self):
        """Otherwise the page takes bookings and fails on every one of them,
        where only the host ever sees it."""
        resp = self.save_page(meeting_mode="teams")
        self.assertIn(b"Teams meetings aren", resp.data)   # not set up here
        self.app.config["MS_CLIENT_ID"] = "cid"
        self.app.config["MS_CLIENT_SECRET"] = "secret"
        self.addCleanup(self.app.config.update, MS_CLIENT_ID="", MS_CLIENT_SECRET="")
        resp = self.save_page(meeting_mode="teams")
        self.assertIn(b"Connect your Microsoft account", resp.data)
        self.assertEqual(self.reload().booking.meeting_mode, "none")

    def test_a_teams_meeting_is_made_for_each_booking(self):
        self.connect_microsoft()
        self.stub_teams(answer="https://teams.example/l/meet-1")
        self.save_page(meeting_mode="teams")
        self.book(self.first_slot())
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.meeting_url, "https://teams.example/l/meet-1")
            self.assertEqual(booking.meeting_provider, "teams")

    def test_a_teams_failure_keeps_the_appointment(self):
        """Same rule as a failed calendar write: the visitor was told yes, so
        the slot stays taken and the host reads what went wrong."""
        self.connect_microsoft()
        self.stub_teams(error="Microsoft answered 403: no licence")
        self.save_page(meeting_mode="teams")
        start = self.first_slot()
        self.assertEqual(self.book(start).status_code, 200)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.meeting_url, "")
            self.assertIn("no licence", booking.meeting_error)
        self.assertNotIn(start,
                         self.client.get("/book/alice/slots?duration=30")
                         .get_json()["slots"])

    def test_disconnecting_stops_the_page_promising_teams(self):
        self.connect_microsoft()
        self.stub_teams(answer="https://teams.example/l/meet-1")
        self.save_page(meeting_mode="teams")
        self.client.post("/settings/booking/microsoft/disconnect",
                         follow_redirects=True)
        user = self.reload()
        self.assertEqual(user.booking.meeting_mode, "none")
        self.assertFalse(user.ms_refresh_token)

    def test_a_link_the_guest_brings_is_the_one_that_counts(self):
        self.save_page(meeting_mode="link",
                       meeting_link="https://meet.example/alice")
        self.book(self.first_slot(), meeting_url="https://zoom.example/bob")
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.meeting_url, "https://zoom.example/bob")
            self.assertEqual(booking.meeting_provider, "guest")

    def test_a_guest_link_means_no_teams_meeting_is_created(self):
        """Creating one nobody will open leaves a stray meeting in the host's
        Teams for every booking."""
        self.connect_microsoft()
        asked = []
        from application.services import msgraph
        real = msgraph.create_meeting
        msgraph.create_meeting = lambda user, **kw: asked.append(kw) or {
            "join_url": "https://teams.example/l/x", "id": "m"}
        self.addCleanup(setattr, msgraph, "create_meeting", real)
        self.save_page(meeting_mode="teams")
        self.book(self.first_slot(), meeting_url="https://zoom.example/bob")
        self.assertEqual(asked, [])
        with self.app.app_context():
            self.assertEqual(self.Booking.objects.get(owner=self.user).meeting_url,
                             "https://zoom.example/bob")

    def test_the_guest_link_has_to_be_an_http_one(self):
        """It is rendered as a clickable link on the host's settings page, so
        a stranger must not be able to put javascript: there."""
        self.save_page()
        start = self.first_slot()
        resp = self.book(start, meeting_url="javascript:alert(1)")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"http:// or https://", resp.data)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 0)
        # …and the slot is still there to try again with.
        self.assertIn(start, self.client.get("/book/alice/slots?duration=30")
                      .get_json()["slots"])

    def test_no_link_from_the_guest_leaves_the_page_in_charge(self):
        self.save_page(meeting_mode="link",
                       meeting_link="https://meet.example/alice")
        self.book(self.first_slot(), meeting_url="")
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.meeting_url, "https://meet.example/alice")
            self.assertEqual(booking.meeting_provider, "link")

    def test_the_calendar_entry_carries_the_link_as_its_location(self):
        """Not only in the description: LOCATION is what a calendar turns into
        a join button, which is the whole point of writing it out."""
        from application.services import calendar_writeback

        wrote = {}
        real = calendar_writeback.create_event
        calendar_writeback.create_event = lambda cal, user, **kw: (
            wrote.update(kw) or {"id": "evt-9"})
        self.addCleanup(setattr, calendar_writeback, "create_event", real)
        self.svc._write_event = self._write          # the real one

        self.save_page(meeting_mode="link",
                       meeting_link="https://meet.example/alice")
        self.book(self.first_slot())
        self.assertEqual(wrote["location"], "https://meet.example/alice")
        self.assertIn("Join: https://meet.example/alice", wrote["description"])

    def test_without_a_meeting_there_is_no_location(self):
        from application.services import calendar_writeback

        wrote = {}
        real = calendar_writeback.create_event
        calendar_writeback.create_event = lambda cal, user, **kw: (
            wrote.update(kw) or {"id": "evt-9"})
        self.addCleanup(setattr, calendar_writeback, "create_event", real)
        self.svc._write_event = self._write

        self.save_page()
        self.book(self.first_slot())
        self.assertEqual(wrote["location"], "")

    def _capture_attachments(self):
        from application.services import mail

        sent = []
        self.app.config["SMTP_HOST"] = "smtp.example"
        self.addCleanup(self.app.config.update, SMTP_HOST="")
        real = mail.send_email
        mail.send_email = lambda to, subject, body, html=None, attachments=None: (
            sent.append((to, body, attachments)) or True)
        self.addCleanup(setattr, mail, "send_email", real)
        return sent

    def test_the_guest_is_invited_to_the_hosts_meeting(self):
        """The guest's invitation is a REQUEST from the host, for the UID of
        the event in the host's calendar, so the acceptance matches it."""
        sent = self._capture_attachments()
        self.save_page(meeting_mode="link",
                       meeting_link="https://meet.example/alice")
        self.book(self.first_slot())

        self.assertEqual([to for to, _b, _a in sent],
                         ["b@example.com", "bob@example.com"])
        for _to, body, _attachments in sent:
            self.assertIn("https://meet.example/alice", body)
        _to, _body, attachments = sent[1]
        self.assertEqual(len(attachments), 1)
        name, mimetype, data = attachments[0]
        self.assertEqual((name, mimetype),
                         ("appointment.ics", "text/calendar; method=REQUEST"))
        data = data.replace(b"\r\n ", b"")
        self.assertIn(b"METHOD:REQUEST", data)
        self.assertIn(b"LOCATION:https://meet.example/alice", data)
        self.assertIn(b"ORGANIZER;CN=book-test:mailto:b@example.com", data)
        self.assertIn(b"mailto:bob@example.com", data)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertIn(f"UID:cairn-booking-{booking.pk}@cairn".encode(), data)

    def test_the_host_gets_no_second_copy_of_an_event_already_written(self):
        """A second copy, dropped into another calendar, is a second event."""
        sent = self._capture_attachments()
        self.save_page()
        self.book(self.first_slot())
        self.assertIsNone(sent[0][2])

    def test_the_host_gets_the_file_when_the_calendar_write_failed(self):
        sent = self._capture_attachments()
        self.svc._write_event = lambda user, page, booking: None
        self.save_page()
        self.book(self.first_slot())
        self.assertEqual(len(sent[0][2]), 1)

    def test_the_hosts_event_is_the_same_meeting(self):
        from application.services import calendar_writeback

        wrote = {}
        real = calendar_writeback.create_event
        calendar_writeback.create_event = lambda cal, user, **kw: (
            wrote.update(kw) or {"id": kw["uid"]})
        self.addCleanup(setattr, calendar_writeback, "create_event", real)
        self.svc._write_event = self._write

        self.save_page()
        self.book(self.first_slot())
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(wrote["uid"], f"cairn-booking-{booking.pk}@cairn")
            self.assertEqual(booking.event_id, wrote["uid"])
        self.assertEqual(wrote["organizer"], ("book-test", "b@example.com"))
        self.assertEqual([p["email"] for p in wrote["attendees"]],
                         ["b@example.com", "bob@example.com"])

    def test_the_organizer_is_the_address_the_calendar_knows(self):
        """Nextcloud knows the host as another address than Cairn does. Apple
        Calendar applies an acceptance only to a meeting organised by one of
        the account's own addresses, so that one is the ORGANIZER everywhere:
        on the event, in the guest's invitation and in the cancellation."""
        from application.services import calendar_writeback

        wrote = {}
        real = calendar_writeback.create_event
        calendar_writeback.create_event = lambda cal, user, **kw: (
            wrote.update(kw) or {"id": kw["uid"]})
        self.addCleanup(setattr, calendar_writeback, "create_event", real)
        self.svc._write_event = self._write
        self.organizer = "host@cloud.example.com"
        sent = self._capture_attachments()

        self.save_page()
        self.book(self.first_slot())
        self.assertEqual(wrote["organizer"], ("book-test", "host@cloud.example.com"))
        self.assertEqual([p["email"] for p in wrote["attendees"]],
                         ["host@cloud.example.com", "bob@example.com"])
        # The host's notice still goes to the Cairn account's address.
        self.assertEqual(sent[0][0], "b@example.com")
        data = sent[1][2][0][2].replace(b"\r\n ", b"")
        self.assertIn(b"ORGANIZER;CN=book-test:mailto:host@cloud.example.com", data)

        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.organizer_email, "host@cloud.example.com")
            sent.clear()
            real_delete = calendar_writeback.delete_event
            calendar_writeback.delete_event = lambda cal, user, event_id: None
            self.addCleanup(setattr, calendar_writeback, "delete_event",
                            real_delete)
            self.svc.cancel(self.User.objects.get(pk=self.user.pk), booking)
        data = sent[0][2][0][2].replace(b"\r\n ", b"")
        self.assertIn(b"METHOD:CANCEL", data)
        self.assertIn(b"ORGANIZER;CN=book-test:mailto:host@cloud.example.com", data)

    def _capture_mail(self):
        from application.services import mail

        sent = []
        self.app.config["SMTP_HOST"] = "smtp.example"
        self.addCleanup(self.app.config.update, SMTP_HOST="")
        real = mail.send_email
        mail.send_email = lambda to, subject, body, html=None, attachments=None: (
            sent.append(to) or True)
        self.addCleanup(setattr, mail, "send_email", real)
        return sent

    def _background_mail(self, enqueue):
        try:
            from application.services import sync_tasks
        except ImportError as exc:  # celery/redis not in this venv
            self.skipTest(f"no Celery here: {exc}")

        self.app.config["BOOKING_MAILS_IN_BACKGROUND"] = True
        self.addCleanup(self.app.config.update, BOOKING_MAILS_IN_BACKGROUND=False)
        real = sync_tasks.enqueue_booking_mails
        sync_tasks.enqueue_booking_mails = enqueue
        self.addCleanup(setattr, sync_tasks, "enqueue_booking_mails", real)

    def test_the_mails_go_to_the_worker_not_into_the_request(self):
        """Two SMTP sessions are seconds the confirmation page waited for."""
        sent = self._capture_mail()
        queued = []
        self._background_mail(queued.append)
        self.save_page()
        self.assertEqual(self.book(self.first_slot()).status_code, 200)
        self.assertEqual(sent, [])
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(queued, [str(booking.pk)])
            # What the worker then runs sends both.
            from application.services.sync_tasks import send_booking_mails
            self.assertEqual(send_booking_mails(str(booking.pk)), "sent")
        self.assertEqual(sent, ["b@example.com", "bob@example.com"])

    def test_no_broker_sends_the_mails_inline(self):
        sent = self._capture_mail()

        def broken(_booking_id):
            raise ConnectionError("no redis")
        self._background_mail(broken)
        self.save_page()
        self.assertEqual(self.book(self.first_slot()).status_code, 200)
        self.assertEqual(sent, ["b@example.com", "bob@example.com"])

    def test_a_booking_called_off_before_the_worker_gets_no_mail(self):
        sent = self._capture_mail()
        self._background_mail(lambda _booking_id: None)
        from application.services.sync_tasks import send_booking_mails
        with self.app.app_context():
            from bson import ObjectId
            self.assertEqual(send_booking_mails(str(ObjectId())), "gone")
        self.assertEqual(sent, [])

    def test_the_booking_asks_the_calendars_only_around_its_slot(self):
        """The page loaded the whole window already; the click re-checks one
        slot, and whether that collides depends on nothing a day away."""
        asked = []
        fake = self.svc.busy_intervals
        self.svc.busy_intervals = lambda user, page, since, until: (
            asked.append((since, until)) or fake(user, page, since, until))
        self.save_page()
        start = self.first_slot()
        asked.clear()
        self.assertEqual(self.book(start).status_code, 200)
        self.assertEqual(len(asked), 1)
        since, until = asked[0]
        self.assertLessEqual(until - since, timedelta(days=3))
        when = datetime.fromisoformat(start.replace("Z", "+00:00"))
        self.assertLess(since, when)
        self.assertGreater(until, when + timedelta(minutes=30))

    def test_a_busy_calendar_beside_the_slot_still_refuses_it(self):
        """The narrower question must still see an event that overlaps."""
        self.save_page()
        start = self.first_slot()
        when = datetime.fromisoformat(start.replace("Z", "+00:00"))
        self.busy = [(when + timedelta(minutes=10), when + timedelta(minutes=20))]
        resp = self.book(start)
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"just taken", resp.data)

    def test_sending_the_same_booking_twice_books_it_once(self):
        """A reload of the confirmation, or a second click while the first
        request still ran, posts the same form again: the visitor gets their
        confirmation back, not "just taken", and nothing happens twice."""
        sent = self._capture_mail()
        self.save_page()
        start = self.first_slot()
        first = self.book(start)
        again = self.book(start)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(again.status_code, 200)
        self.assertIn(b"You're booked", again.data)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 1)
        self.assertEqual(len(self.written), 1)
        self.assertEqual(sent, ["b@example.com", "bob@example.com"])
        bell = [n for n in self.reload().notifications if n.kind == "booking"]
        self.assertEqual(len(bell), 1)

    # -- the directory and the portrait -----------------------------------
    def test_the_directory_lists_a_live_page(self):
        self.save_page()
        body = self.client.get("/book/").data
        self.assertIn(b"book-test", body)
        self.assertIn(b"/book/alice", body)

    def test_a_page_whose_document_predates_the_switch_is_still_listed(self):
        """``listed`` defaults to True, and a field at its default is never
        written — so the stored document has no such key at all. Querying for
        it in Mongo found nobody; the default only exists after a load."""
        self.save_page()
        with self.app.app_context():
            self.User._get_collection().update_one(
                {"username": "book-test"}, {"$unset": {"booking.listed": ""}})
        self.assertIn(b"/book/alice", self.client.get("/book/").data)

    def test_a_page_that_asks_not_to_be_listed_is_not(self):
        self.save_page(listed="")
        self.assertNotIn(b"/book/alice", self.client.get("/book/").data)
        # …and is still reachable for anyone holding the link.
        self.assertEqual(self.client.get("/book/alice").status_code, 200)

    def test_a_page_that_is_switched_off_is_not_listed(self):
        self.save_page(enabled="")
        self.assertNotIn(b"/book/alice", self.client.get("/book/").data)

    def test_a_photo_is_stored_square_and_served_as_webp(self):
        self.save_page(photo=(io.BytesIO(png(600, 300)), "me.png"))
        resp = self.client.get("/book/alice/photo")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/webp")
        from PIL import Image
        img = Image.open(io.BytesIO(resp.data))
        self.assertEqual(img.size, (320, 320))

    def test_the_chosen_crop_is_what_gets_stored(self):
        """The browser reports the rectangle; the server cuts it. Picking the
        green quarter must not give back the middle of the whole picture."""
        self.save_page(photo=(io.BytesIO(quartered_png(400)), "q.png"),
                       crop_x="200", crop_y="0", crop_size="200")
        data = self.client.get("/book/alice/photo").data
        self.assertTrue(looks_like(middle_pixel(data), (0, 255, 0)),
                        middle_pixel(data))

    def test_without_a_crop_the_middle_is_taken(self):
        self.save_page(photo=(io.BytesIO(quartered_png(400)), "q.png"))
        # The centre of a four-colour square sits on the seam, so the test is
        # simply that it is NOT one quarter's flat colour.
        self.assertFalse(looks_like(
            middle_pixel(self.client.get("/book/alice/photo").data), (0, 255, 0)))

    def test_a_crop_outside_the_image_is_pulled_back_in(self):
        """Numbers from a form are numbers from anyone."""
        self.save_page(photo=(io.BytesIO(quartered_png(400)), "q.png"),
                       crop_x="9999", crop_y="-500", crop_size="99999")
        resp = self.client.get("/book/alice/photo")
        self.assertEqual(resp.status_code, 200)
        from PIL import Image
        self.assertEqual(Image.open(io.BytesIO(resp.data)).size, (320, 320))

    def test_a_nonsense_crop_falls_back_to_the_middle(self):
        self.save_page(photo=(io.BytesIO(quartered_png(400)), "q.png"),
                       crop_x="abc", crop_y="", crop_size="0")
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 200)

    def test_no_photo_is_a_404_not_a_broken_page(self):
        self.save_page()
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 404)
        self.assertEqual(self.client.get("/book/alice").status_code, 200)

    def test_a_file_that_is_not_an_image_is_refused(self):
        resp = self.save_page(photo=(io.BytesIO(b"<svg onload=alert(1)>"), "x.svg"))
        # The apostrophe in the message is HTML-escaped by the time it renders.
        self.assertIn(b"a PNG, JPEG, GIF or WebP image", resp.data)
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 404)

    def test_a_photo_can_be_removed_again(self):
        self.save_page(photo=(io.BytesIO(png(64, 64)), "me.png"))
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 200)
        self.save_page(remove_photo="y")
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 404)

    # -- embedding on someone else's site ---------------------------------
    def test_the_embed_may_be_framed_by_anyone(self):
        """The proxy sends X-Frame-Options: SAMEORIGIN for everything; a
        browser that sees frame-ancestors ignores it, which is why the policy
        has to come from here."""
        self.save_page()
        resp = self.client.get("/book/alice?embed=1")
        self.assertEqual(resp.headers["Content-Security-Policy"],
                         "frame-ancestors *")
        self.assertNotIn("X-Frame-Options", resp.headers)

    def test_the_page_on_its_own_is_not_framable(self):
        """Only the embed is opened up; the page on Cairn's own address keeps
        the same-origin rule."""
        self.save_page()
        resp = self.client.get("/book/alice")
        self.assertEqual(resp.headers["Content-Security-Policy"],
                         "frame-ancestors 'self'")
        self.assertEqual(resp.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_the_form_post_inside_the_frame_stays_framable(self):
        self.save_page()
        resp = self.client.post("/book/alice/book?embed=1&bare=1", data={
            "start": self.first_slot(), "duration": "30", "name": "Bob",
            "email": "bob@example.com", "note": "", "tz": "Europe/Berlin",
            "form_token": self.form_token()})
        self.assertEqual(resp.headers["Content-Security-Policy"],
                         "frame-ancestors *")

    def test_the_allowed_ancestors_are_configurable(self):
        self.save_page()
        old = self.app.config["BOOKING_FRAME_ANCESTORS"]
        self.app.config["BOOKING_FRAME_ANCESTORS"] = "https://example.com"
        try:
            self.assertEqual(
                self.client.get("/book/alice?embed=1").headers["Content-Security-Policy"],
                "frame-ancestors https://example.com")
        finally:
            self.app.config["BOOKING_FRAME_ANCESTORS"] = old

    def test_the_rest_of_the_app_is_not_made_framable(self):
        self.assertNotIn("Content-Security-Policy",
                         self.client.get("/settings/booking").headers)

    def test_embed_mode_drops_the_chrome_and_reports_its_height(self):
        self.save_page()
        plain = self.client.get("/book/alice").get_data(as_text=True)
        embed = self.client.get("/book/alice?embed=1").get_data(as_text=True)
        self.assertNotIn("cairnBooking", plain)
        self.assertIn("cairnBooking", embed)
        self.assertIn('<body class="embed">', embed)
        # The form has to stay inside the frame, or confirming navigates the
        # host page instead of the widget.
        self.assertIn("/book/alice/book?embed=1", embed)

    def test_the_confirmation_stays_embedded_too(self):
        self.save_page()
        resp = self.client.post("/book/alice/book?embed=1", data={
            "start": self.first_slot(), "duration": "30", "name": "Bob",
            "email": "bob@example.com", "note": "", "tz": "Europe/Berlin",
            "form_token": self.form_token()})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('<body class="embed">', body)
        self.assertIn("cairnBooking", body)

    def test_the_standalone_page_stays_dark_and_the_embed_asks_the_visitor(self):
        self.save_page()
        plain = self.client.get("/book/alice").get_data(as_text=True)
        self.assertIn('data-bs-theme="dark"', plain)
        self.assertNotIn("prefers-color-scheme", plain)
        embed = self.client.get("/book/alice?embed=1").get_data(as_text=True)
        self.assertIn("prefers-color-scheme", embed)

    def test_a_theme_in_the_url_wins(self):
        self.save_page()
        light = self.client.get("/book/alice?embed=1&theme=light").get_data(as_text=True)
        self.assertIn('data-bs-theme="light"', light)
        self.assertNotIn("prefers-color-scheme", light)
        dark = self.client.get("/book/alice?embed=1&theme=dark").get_data(as_text=True)
        self.assertIn('data-bs-theme="dark"', dark)
        self.assertNotIn("prefers-color-scheme", dark)

    def test_a_nonsense_theme_is_ignored(self):
        self.save_page()
        body = self.client.get("/book/alice?embed=1&theme=pink").get_data(as_text=True)
        self.assertIn("prefers-color-scheme", body)

    def test_a_bare_embed_shows_only_the_booking(self):
        self.save_page(title="Talk to me", intro="Hi there.")
        normal = self.client.get("/book/alice?embed=1").get_data(as_text=True)
        self.assertIn("Talk to me", normal)
        self.assertIn("Powered by", normal)
        bare = self.client.get("/book/alice?embed=1&bare=1").get_data(as_text=True)
        self.assertIn('<body class="embed bare">', bare)
        self.assertNotIn('class="book-title', bare)
        self.assertNotIn("Hi there.", bare)
        self.assertNotIn("Powered by", bare)
        self.assertIn('id="slots"', bare)
        self.assertIn("cairnBooking", bare)
        # The form carries the dress, so the confirmation is bare too.
        self.assertIn("/book/alice/book?embed=1&amp;bare=1", bare)

    def test_the_options_do_nothing_outside_an_embed(self):
        self.save_page()
        plain = self.client.get("/book/alice?bare=1&compact=1&accent=ff0000"
                                ).get_data(as_text=True)
        self.assertIn('<body class="">', plain)
        self.assertIn("Powered by", plain)
        self.assertNotIn("#ff0000", plain)

    def test_an_accent_colours_the_buttons_and_nonsense_is_dropped(self):
        self.save_page()
        body = self.client.get("/book/alice?embed=1&accent=%234F8A2C"
                               ).get_data(as_text=True)
        self.assertIn("--bs-primary: #4f8a2c", body)
        self.assertIn("79, 138, 44", body)
        self.assertIn("accent=4f8a2c", body)
        evil = self.client.get("/book/alice?embed=1&accent=red;}body{x"
                               ).get_data(as_text=True)
        self.assertNotIn("--bs-primary: #", evil)
        self.assertNotIn("body{x", evil)

    def test_compact_tightens_the_page(self):
        self.save_page()
        body = self.client.get("/book/alice?embed=1&compact=1").get_data(as_text=True)
        self.assertIn('<body class="embed compact">', body)

    def test_lang_de_speaks_german_down_to_the_confirmation(self):
        self.save_page()
        page = self.client.get("/book/alice?embed=1&bare=1&lang=de"
                               ).get_data(as_text=True)
        self.assertIn('<html lang="de"', page)
        self.assertIn("Zeit wählen", page)
        self.assertIn("Termin buchen", page)
        self.assertIn('data-lang="de"', page)
        self.assertIn("Tage mit freien Zeiten", page)   # the script's wording
        resp = self.client.post("/book/alice/book?embed=1&bare=1&lang=de", data={
            "start": self.first_slot(), "duration": "30", "name": "Bob",
            "email": "bob@example.com", "note": "", "tz": "Europe/Berlin",
            "form_token": self.form_token()})
        done = resp.get_data(as_text=True)
        self.assertIn("Ihr Termin steht", done)
        self.assertIn('<body class="embed bare">', done)
        english = self.client.get("/book/alice?embed=1&lang=xx").get_data(as_text=True)
        self.assertIn("Pick a time", english)
        self.assertIn('<html lang="en"', english)

    def test_an_embed_is_transparent_all_the_way_up(self):
        """app.css paints html too; a transparent body alone still shows a
        box inside the host site."""
        self.save_page()
        body = self.client.get("/book/alice?embed=1").get_data(as_text=True)
        self.assertIn('class="embed"', body.split("<head>")[0])
        self.assertIn("html.embed, body.embed { background: transparent; }", body)

    def test_the_settings_page_hands_out_a_snippet(self):
        self.save_page()
        body = self.client.get("/settings/booking").get_data(as_text=True)
        self.assertIn("/book/alice?embed=1", body)
        self.assertIn("cairn-booking", body)

    def test_the_day_cap_closes_the_day_for_everyone(self):
        self.save_page(max_per_day="1")
        start = self.first_slot()
        self.book(start)
        day = datetime.fromisoformat(start).date()
        left = [s for s in self.client.get("/book/alice/slots?duration=30")
                .get_json()["slots"]
                if datetime.fromisoformat(s).date() == day]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
