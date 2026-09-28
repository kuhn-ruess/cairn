"""Retention of the navbar bell's notifications.

The rule has one edge worth pinning: age only removes a notification the user
has actually seen. An unread one waits, however old it is — only the cap can
push it out.
"""
import unittest
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from application.models.user import User


def _aged(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


class PruneNotificationsTest(unittest.TestCase):
    def _user(self, *entries):
        user = User(username="tester")
        for text, read, age in entries:
            user.add_notification(text=text, kind="assigned")
            user.notifications[-1].read = read
            user.notifications[-1].created_at = _aged(age)
        return user

    def test_read_and_old_goes(self):
        user = self._user(("old", True, User.NOTIFICATION_KEEP_DAYS + 1))
        self.assertTrue(user.prune_notifications())
        self.assertEqual([], list(user.notifications))

    def test_read_and_recent_stays(self):
        user = self._user(("recent", True, User.NOTIFICATION_KEEP_DAYS - 1))
        self.assertFalse(user.prune_notifications())
        self.assertEqual(1, len(user.notifications))

    def test_unread_never_expires(self):
        user = self._user(("ancient", False, User.NOTIFICATION_KEEP_DAYS * 10))
        self.assertFalse(user.prune_notifications())
        self.assertEqual(1, len(user.notifications))
        self.assertEqual(1, user.unread_notification_count)

    def test_naive_timestamp_is_read_as_utc(self):
        """Older documents can come back without a timezone; comparing one
        against an aware cutoff would raise rather than expire it."""
        user = self._user(("old", True, User.NOTIFICATION_KEEP_DAYS + 1))
        user.notifications[0].created_at = \
            user.notifications[0].created_at.replace(tzinfo=None)
        self.assertTrue(user.prune_notifications())

    def test_mark_read_reports_a_prune_as_a_change(self):
        """The view saves only when this says something changed — an expiry
        with nothing left to mark read still has to be written."""
        user = self._user(("old", True, User.NOTIFICATION_KEEP_DAYS + 1))
        self.assertTrue(user.mark_notifications_read())
        self.assertEqual([], list(user.notifications))

    def test_cap_still_holds(self):
        user = User(username="tester")
        for i in range(User.NOTIFICATION_CAP + 5):
            user.add_notification(text=f"n{i}", kind="assigned")
        self.assertEqual(User.NOTIFICATION_CAP, len(user.notifications))
        self.assertEqual("n5", user.notifications[0].text)


class NotificationFeedTest(unittest.TestCase):
    """The bell shows one row per task — the newest entry — unread rows on
    top; the API keeps the raw entries (that is test_notifications_api)."""

    def _user(self, *entries):
        user = User(username="tester")
        for text, item, read in entries:
            user.add_notification(text=text, kind="comment", item_id=item)
            user.notifications[-1].read = read
        return user

    def test_same_task_collapses_to_the_newest(self):
        a = ObjectId()
        user = self._user(("assigned", a, False), ("comment 1", a, False),
                          ("comment 2", a, False))
        feed = user.notification_feed
        self.assertEqual(["comment 2"], [r.n.text for r in feed])
        self.assertEqual(3, len(feed[0].ids))
        self.assertEqual(1, user.unread_notification_row_count)
        self.assertEqual(3, user.unread_notification_count)   # raw, API

    def test_unread_rows_come_first(self):
        a, b, c = ObjectId(), ObjectId(), ObjectId()
        user = self._user(("a unread", a, False), ("b read", b, True),
                          ("c unread", c, False))
        user.add_notification(text="invite", kind="team_invite")
        self.assertEqual(["invite", "c unread", "a unread", "b read"],
                         [r.n.text for r in user.notification_feed])

    def test_a_row_is_unread_while_any_entry_is(self):
        a = ObjectId()
        user = self._user(("older unread", a, False), ("newer read", a, True))
        (row,) = user.notification_feed
        self.assertTrue(row.unread)
        self.assertEqual("newer read", row.n.text)

    def test_marking_the_row_marks_the_hidden_entries(self):
        a, b = ObjectId(), ObjectId()
        user = self._user(("a1", a, False), ("b1", b, False), ("a2", a, False))
        row = next(r for r in user.notification_feed if r.n.text == "a2")
        user.mark_notifications_read(row.ids)
        self.assertEqual(["b1"], [n.text for n in user.notifications if not n.read])
        self.assertEqual(1, user.unread_notification_row_count)

    def test_entries_without_an_item_stay_apart(self):
        user = User(username="tester")
        user.add_notification(text="invite 1", kind="team_invite")
        user.add_notification(text="invite 2", kind="team_invite")
        self.assertEqual(2, len(user.notification_feed))


if __name__ == "__main__":
    unittest.main()
