"""A project page always offers the public link — and the link it makes works.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Why this exists: the whole feature hangs off ONE icon-only dropdown in the
project page's toolbar. Model field, route and public view can all stay healthy
while that entry point silently stops rendering — a template edit in the
toolbar, a changed owner check, a moved button — and nothing else in the app
would notice. That is exactly the failure that gets reported as "the option to
create a public link is gone", so the entry point itself is pinned here:

* the owner's project page carries the enable form (the only way in),
* posting it mints a token and flips ``share_enabled``,
* the page then shows the real ``/p/<token>`` URL plus regenerate/disable,
* that URL answers anonymously,
* and somebody who merely has access to the project is not offered it.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ProjectPublicLinkTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()  # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            from application.models.item import Item
            from application.models.user import User
            Item.all_objects.delete()
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="share-owner", email="owner@test.invalid",
                             password_hash="x", level="premium").save()
            self.other = User(username="share-guest", email="guest@test.invalid",
                              password_hash="x", level="premium").save()
            self.project = Item(owner=self.user, kind="project",
                                title="Kundenprojekt", status="active",
                                members=[self.other]).save()
            Item(owner=self.user, kind="task", parent=self.project,
                 title="something to show the customer").save()
        self.client = self.app.test_client()
        self.login(self.client, self.user)

    @staticmethod
    def login(client, user):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True

    def page(self):
        resp = self.client.get(f"/projects/{self.project.pk}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    # -- the entry point --------------------------------------------------

    def test_the_project_page_offers_the_public_link(self):
        html = self.page()
        self.assertIn(f"/projects/{self.project.pk}/share", html,
                      "the project toolbar no longer posts to the share route")
        self.assertIn("Enable public link", html,
                      "the button that creates the public link is gone")

    def test_enabling_mints_a_token_and_the_page_shows_the_link(self):
        resp = self.client.post(f"/projects/{self.project.pk}/share",
                                data={"action": "enable"})
        self.assertEqual(resp.status_code, 302)
        self.project.reload()
        self.assertTrue(self.project.share_enabled)
        self.assertTrue(self.project.share_token)

        html = self.page()
        self.assertIn(f"/p/{self.project.share_token}", html,
                      "the project page does not show the link it just created")
        # The link is only useful if it can also be replaced and revoked.
        self.assertIn('value="regenerate"', html)
        self.assertIn('value="disable"', html)

    def test_the_link_opens_without_a_login(self):
        self.client.post(f"/projects/{self.project.pk}/share",
                         data={"action": "enable"})
        self.project.reload()
        anon = self.app.test_client()
        resp = anon.get(f"/p/{self.project.share_token}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("something to show the customer", resp.get_data(as_text=True))

    def test_disabling_takes_the_page_down_again(self):
        self.client.post(f"/projects/{self.project.pk}/share",
                         data={"action": "enable"})
        self.project.reload()
        token = self.project.share_token
        self.client.post(f"/projects/{self.project.pk}/share",
                         data={"action": "disable"})
        anon = self.app.test_client()
        self.assertEqual(anon.get(f"/p/{token}").status_code, 404)

    # -- who is offered it ------------------------------------------------

    def test_a_member_may_read_the_project_but_not_publish_it(self):
        guest = self.app.test_client()
        self.login(guest, self.other)
        html = guest.get(f"/projects/{self.project.pk}").get_data(as_text=True)
        self.assertNotIn("Enable public link", html)
        self.assertEqual(
            guest.post(f"/projects/{self.project.pk}/share",
                       data={"action": "enable"}).status_code, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
