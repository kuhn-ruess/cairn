"""Retention of the navbar bell's notifications.

The rule has one edge worth pinning: age only removes a notification the user
has actually seen. An unread one waits, however old it is — only the cap can
push it out.
"""
import unittest
from datetime import datetime, timedelta, timezone

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


if __name__ == "__main__":
    unittest.main()
