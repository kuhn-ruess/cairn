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

    def book(self, start, duration=30, **over):
        data = {"start": start, "duration": str(duration), "name": "Bob",
                "email": "bob@example.com", "note": "Hallo", "tz": "America/New_York"}
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
        resp = self.book(start)
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

    def test_both_confirmations_carry_the_invitation(self):
        from application.services import mail

        sent = []
        self.app.config["SMTP_HOST"] = "smtp.example"
        self.addCleanup(self.app.config.update, SMTP_HOST="")
        real = mail.send_email
        mail.send_email = lambda to, subject, body, html=None, attachments=None: (
            sent.append((to, body, attachments)) or True)
        self.addCleanup(setattr, mail, "send_email", real)

        self.save_page(meeting_mode="link",
                       meeting_link="https://meet.example/alice")
        self.book(self.first_slot())

        self.assertEqual([to for to, _b, _a in sent],
                         ["b@example.com", "bob@example.com"])
        for _to, body, attachments in sent:
            self.assertIn("https://meet.example/alice", body)
            self.assertEqual(len(attachments), 1)
            name, mimetype, data = attachments[0]
            self.assertEqual((name, mimetype), ("appointment.ics", "text/calendar"))
            self.assertIn(b"BEGIN:VEVENT", data)
            self.assertIn(b"LOCATION:https://meet.example/alice", data)

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
    def test_the_page_may_be_framed_by_anyone(self):
        """The proxy sends X-Frame-Options: SAMEORIGIN for everything; a
        browser that sees frame-ancestors ignores it, which is why the policy
        has to come from here."""
        self.save_page()
        resp = self.client.get("/book/alice")
        self.assertEqual(resp.headers["Content-Security-Policy"],
                         "frame-ancestors *")
        self.assertNotIn("X-Frame-Options", resp.headers)

    def test_the_allowed_ancestors_are_configurable(self):
        self.save_page()
        old = self.app.config["BOOKING_FRAME_ANCESTORS"]
        self.app.config["BOOKING_FRAME_ANCESTORS"] = "https://example.com"
        try:
            self.assertEqual(
                self.client.get("/book/alice").headers["Content-Security-Policy"],
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
            "email": "bob@example.com", "note": "", "tz": "Europe/Berlin"})
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
