"""A tag is shown by its name, never as "@name".

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Tags used to carry an ``is_context`` flag that put an "@" in front of the
name. "@" now means a person everywhere (quick add, comments), so the flag is
gone: a stored tag that still carries it loads and shows plainly, the API
neither returns it nor fails when an old client sends it.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class TagNoContextTests(unittest.TestCase):

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
            from application.models.user import User
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.user import User
        self.User = User
        with self.app.app_context():
            User.objects.delete()
            self.user = User(username="ctx-test", email="ctx@test.invalid",
                             password_hash="x", level="premium").save()
            # An old document: the flag is stored as it used to be.
            User._get_collection().update_one(
                {"_id": self.user.pk},
                {"$set": {"tags": [{"name": "Telefon", "color": "#6c757d",
                                    "is_context": True}]}})
            _, self.token = User.objects.get(pk=self.user.pk).issue_api_token("test")
        self.client = self.app.test_client()
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def test_an_old_context_tag_loads_and_shows_plainly(self):
        with self.app.app_context():
            user = self.User.objects.get(pk=self.user.pk)
            self.assertEqual(str(user.tag_by_name("Telefon")), "Telefon")

    def test_the_api_does_not_return_the_flag(self):
        r = self.client.get("/api/v1/tags", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        tags = body if isinstance(body, list) else body.get("tags", body.get("items"))
        self.assertTrue(tags)
        for tag in tags:
            self.assertNotIn("is_context", tag)

    def test_the_api_ignores_the_flag_on_input(self):
        r = self.client.post("/api/v1/tags", headers=self.auth,
                             json={"name": "Büro", "is_context": True})
        self.assertEqual(r.status_code, 201)
        self.assertNotIn("is_context", r.get_json())
        r = self.client.patch("/api/v1/tags/Telefon", headers=self.auth,
                              json={"is_context": False, "color": "#ff0000"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["color"], "#ff0000")


if __name__ == "__main__":
    unittest.main()
