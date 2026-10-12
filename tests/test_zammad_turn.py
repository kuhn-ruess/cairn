"""services/zammad_import — whose move it is on a ticket.

You plan the task, work it, answer the ticket; the topic isn't finished, but
until the customer writes back there is nothing to do. The sync reads that off
Zammad's two contact stamps, so the task leaves Today on its own and returns in
Next when the answer comes. Internal notes move neither stamp.

Run with ``python -m unittest discover tests``.
"""
import unittest

from application.services.zammad_import import _turn_status

EARLY = "2026-09-30T08:00:00.000Z"
LATE = "2026-09-30T10:00:00.000Z"


class TurnStatus(unittest.TestCase):

    def test_unanswered_ticket_says_nothing(self):
        # New inbound work stays "open" — untriaged, not a next action yet.
        self.assertIsNone(_turn_status({"last_contact_customer_at": LATE}))
        self.assertIsNone(_turn_status({}))

    def test_answer_out_last_is_waiting(self):
        self.assertEqual(_turn_status({
            "last_contact_customer_at": EARLY,
            "last_contact_agent_at": LATE,
        }), "waiting_reply")

    def test_answer_without_any_customer_contact_is_waiting(self):
        # A ticket the agent opened (outbound mail, phone note).
        self.assertEqual(
            _turn_status({"last_contact_agent_at": LATE}), "waiting_reply")

    def test_customer_wrote_back_is_next(self):
        self.assertEqual(_turn_status({
            "last_contact_agent_at": EARLY,
            "last_contact_customer_at": LATE,
        }), "next")

    def test_unreadable_agent_stamp_says_nothing(self):
        self.assertIsNone(_turn_status({
            "last_contact_agent_at": "garbage",
            "last_contact_customer_at": LATE,
        }))


if __name__ == "__main__":
    unittest.main()
