"""Files of any type go onto a task over the REST API.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Until this existed the API could only download attachments: an agent that had
made a presentation for a task had no way to hand it over. Pinned here: the
upload and the comment with files, the limits (10 MB per file, the item cap,
all or nothing), who may do it, and how a file is served back, because the
stored type is whatever the uploader claimed.
"""
import io
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    connect = None

ODP = "application/vnd.oasis.opendocument.presentation"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@unittest.skipIf(connect is None, "mongoengine not installed")
class ApiAttachmentTests(unittest.TestCase):

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
            self.owner = User(username="at-owner", email="ato@test.invalid",
                              password_hash="x", level="premium").save()
            self.agent = User(username="at-agent", email="ata@test.invalid",
                              password_hash="x", level="premium").save()
            self.stranger = User(username="at-x", email="atx@test.invalid",
                                 password_hash="x", level="premium").save()
            self.task = Item(owner=self.owner, kind="task", title="Vortrag",
                             assigned_to=self.agent, note="Folien bauen").save()
            self.tok = {u.username: u.issue_api_token("t")[1]
                        for u in (self.owner, self.agent, self.stranger)}
            self.ro = self.owner.issue_api_token("ro", read_only=True)[1]
        self.client = self.app.test_client()

    # -- helpers ---------------------------------------------------------

    def _h(self, who="at-agent"):
        return {"Authorization": f"Bearer {self.tok[who]}"}

    def _upload(self, data=b"PK\x03\x04odp", name="Vortrag.odp", who="at-agent",
                ctype="application/octet-stream", **form):
        return self.client.post(
            f"/api/v1/tasks/{self.task.pk}/attachments",
            data={"file": (io.BytesIO(data), name, ctype), **form},
            headers=self._h(who), content_type="multipart/form-data")

    def _comment(self, files, body="Hier die Folien", who="at-agent"):
        data = {"body": body,
                "file": [(io.BytesIO(d), n, "application/octet-stream")
                         for d, n in files]}
        return self.client.post(
            f"/api/v1/tasks/{self.task.pk}/comments", data=data,
            headers=self._h(who), content_type="multipart/form-data")

    def _fresh(self):
        with self.app.app_context():
            return self.Item.objects(pk=self.task.pk).first()

    # -- upload ----------------------------------------------------------

    def test_any_file_type_is_stored_and_linked_in_the_note(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, 201, resp.json)
        body = resp.json
        self.assertEqual(body["filename"], "Vortrag.odp")
        # curl sends application/octet-stream; the name says what it is.
        self.assertEqual(body["content_type"], ODP)
        self.assertEqual(body["url"],
                         f"/api/v1/tasks/{self.task.pk}/attachments/{body['token']}")
        self.assertEqual(
            body["markdown"],
            f"[Vortrag.odp](/tasks/{self.task.pk}/note-file/{body['token']})")
        task = self._fresh()
        self.assertEqual([f.token for f in task.note_files], [body["token"]])
        self.assertTrue(task.note.endswith(body["markdown"]))
        self.assertTrue(task.note.startswith("Folien bauen"))

    def test_append_to_note_0_leaves_the_note_alone(self):
        resp = self._upload(append_to_note="0")
        self.assertEqual(resp.status_code, 201)
        task = self._fresh()
        self.assertEqual(task.note, "Folien bauen")
        self.assertEqual(len(task.note_files), 1)

    def test_a_path_in_the_filename_is_stripped(self):
        resp = self._upload(name='..\\..\\etc/passwd.txt')
        self.assertEqual(resp.json["filename"], "passwd.txt")

    def test_no_file_and_empty_file_are_refused(self):
        resp = self.client.post(f"/api/v1/tasks/{self.task.pk}/attachments",
                                data={}, headers=self._h(),
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._upload(data=b"").status_code, 400)

    def test_a_file_over_10_mb_is_refused(self):
        from application.views.tasks import NOTE_FILE_MAX_BYTES
        resp = self._upload(data=b"x" * (NOTE_FILE_MAX_BYTES + 1))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(len(self._fresh().note_files), 0)

    def test_a_full_task_takes_no_more(self):
        from application.views.tasks import NOTE_FILE_MAX_BYTES
        chunk = b"x" * NOTE_FILE_MAX_BYTES
        self.assertEqual(self._upload(data=chunk, name="a.bin").status_code, 201)
        resp = self._upload(data=chunk, name="b.bin")
        self.assertEqual(resp.status_code, 413)
        self.assertEqual([f.filename for f in self._fresh().note_files], ["a.bin"])

    # -- who may ---------------------------------------------------------

    def test_owner_and_assignee_may_a_stranger_may_not(self):
        self.assertEqual(self._upload(who="at-owner").status_code, 201)
        self.assertEqual(self._upload(who="at-agent").status_code, 201)
        self.assertEqual(self._upload(who="at-x").status_code, 404)
        self.assertEqual(self._comment([(b"x", "a.txt")], who="at-x").status_code, 404)
        self.assertEqual(len(self._fresh().note_files), 2)

    def test_a_read_only_token_uploads_nothing(self):
        resp = self.client.post(
            f"/api/v1/tasks/{self.task.pk}/attachments",
            data={"file": (io.BytesIO(b"x"), "a.txt")},
            headers={"Authorization": f"Bearer {self.ro}"},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(self._fresh().note_files), 0)

    # -- comment with files ----------------------------------------------

    def test_a_comment_carries_its_files_as_links(self):
        resp = self._comment([(b"PK\x03\x04", "Vortrag.odp"), (PNG, "bild.png")])
        self.assertEqual(resp.status_code, 201, resp.json)
        task = self._fresh()
        files = {f.filename: f for f in task.note_files}
        self.assertEqual(set(files), {"Vortrag.odp", "bild.png"})
        body = task.comments[-1].body
        pid = task.pk
        self.assertTrue(body.startswith("Hier die Folien\n\n"))
        self.assertIn(f"[Vortrag.odp](/tasks/{pid}/note-file/"
                      f"{files['Vortrag.odp'].token})", body)
        self.assertIn(f"![bild.png](/tasks/{pid}/note-file/"
                      f"{files['bild.png'].token})", body)
        # API readers get the API download URL in the comment.
        api_body = resp.json["comments"][-1]["body"]
        self.assertIn(f"/api/v1/tasks/{pid}/attachments/"
                      f"{files['Vortrag.odp'].token}", api_body)
        # Referenced by the comment, so a later save keeps them for good.
        with self.app.app_context():
            self.assertEqual(task._prune_orphan_attachments(grace_seconds=0), 0)

    def test_files_without_body_are_a_comment_too(self):
        resp = self._comment([(b"x", "log.txt")], body="")
        self.assertEqual(resp.status_code, 201)
        body = self._fresh().comments[-1].body
        self.assertTrue(body.startswith("[log.txt]("))

    def test_neither_body_nor_file_is_refused(self):
        resp = self._comment([], body="")
        self.assertEqual(resp.status_code, 400)

    def test_json_comment_still_works(self):
        resp = self.client.post(f"/api/v1/tasks/{self.task.pk}/comments",
                                json={"body": "nur Text"}, headers=self._h())
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self._fresh().comments[-1].body, "nur Text")

    def test_one_refused_file_stores_nothing(self):
        from application.views.tasks import NOTE_FILE_MAX_BYTES
        resp = self._comment([(b"ok", "a.txt"),
                              (b"x" * (NOTE_FILE_MAX_BYTES + 1), "big.bin")])
        self.assertEqual(resp.status_code, 413)
        self.assertIn("big.bin", resp.json["error"])
        task = self._fresh()
        self.assertEqual(len(task.note_files), 0)
        self.assertEqual(len(task.comments), 0)

    # -- serving ---------------------------------------------------------

    def _get(self, token, who="at-owner"):
        return self.client.get(
            f"/api/v1/tasks/{self.task.pk}/attachments/{token}", headers=self._h(who))

    def test_an_office_file_downloads(self):
        token = self._upload().json["token"]
        resp = self._get(token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"PK\x03\x04odp")
        self.assertEqual(resp.mimetype, ODP)
        self.assertTrue(resp.headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn('filename="Vortrag.odp"', resp.headers["Content-Disposition"])
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")

    def test_script_capable_types_never_open_inline(self):
        for name, ctype in (("x.html", "text/html"), ("x.svg", "image/svg+xml"),
                            ("x.xml", "application/rss+xml"),
                            ("x.js", "text/javascript")):
            token = self._upload(data=b"<script>1</script>", name=name,
                                 ctype=ctype).json["token"]
            resp = self._get(token)
            self.assertTrue(resp.headers["Content-Disposition"].startswith("attachment;"),
                            name)
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")

    def test_pdf_and_pictures_open_in_the_browser(self):
        for data, name in ((b"%PDF-1.4", "a.pdf"), (PNG, "a.png")):
            token = self._upload(data=data, name=name).json["token"]
            resp = self._get(token)
            self.assertTrue(resp.headers["Content-Disposition"].startswith("inline;"),
                            name)

    def test_the_assignee_can_fetch_a_stranger_cannot(self):
        token = self._upload(who="at-owner").json["token"]
        self.assertEqual(self._get(token, who="at-agent").status_code, 200)
        self.assertEqual(self._get(token, who="at-x").status_code, 404)

    # -- the app shows it as a file ----------------------------------------

    def test_an_attachment_link_renders_with_its_marker(self):
        md = f"[Vortrag.odp](/tasks/{self.task.pk}/note-file/abc_-1)"
        with self.app.test_request_context():
            html = self.app.jinja_env.filters["markdown"](md + " [x](/tasks/1)")
        self.assertIn('class="attachment-link"', html)
        self.assertEqual(html.count("attachment-link"), 1)

    def test_the_spec_lists_the_upload(self):
        spec = self.client.get("/api/v1/openapi.json").json
        self.assertIn("post", spec["paths"]["/tasks/{task_id}/attachments"])
        self.assertIn("multipart/form-data", spec["paths"]["/tasks/{task_id}/comments"]
                      ["post"]["requestBody"]["content"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
