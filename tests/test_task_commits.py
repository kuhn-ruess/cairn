"""Commits registered on a task, so a review can jump straight to the change.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Pinned here: the remote to web URL conversion (credentials never reach a
link), the dedupe on (repo, sha) with short and full hashes, the API's
visibility (owner, assignee; 404 for a stranger, read-only token refused),
commits sent with a comment, and the task page rendering the hash linked or,
without a known link, as copyable text.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    connect = None

FULL = "0123456789abcdef0123456789abcdef01234567"


class WebBaseTests(unittest.TestCase):
    """Pure: no app, no database."""

    def test_remotes_become_web_pages(self):
        from application.services.commits import web_base
        cases = {
            "git@github.com:example-org/tool.git": "https://github.com/example-org/tool",
            "ssh://git@gitlab.example.com:2222/grp/sub/tool.git":
                "https://gitlab.example.com/grp/sub/tool",
            "https://user:secret@git.example.com/org/tool.git":
                "https://git.example.com/org/tool",
            "https://git.example.com:3000/org/tool": "https://git.example.com:3000/org/tool",
            "http://forge.example.com/org/tool/": "http://forge.example.com/org/tool",
        }
        for remote, want in cases.items():
            self.assertEqual(web_base(remote), want, remote)

    def test_no_link_for_what_a_browser_cannot_open(self):
        from application.services.commits import web_base
        for remote in ("", "/srv/git/tool.git", "file:///srv/git/tool.git",
                       "../tool", "javascript:alert(1)", "git@github.com:tool"):
            self.assertIsNone(web_base(remote), remote)

    def test_templates(self):
        from application.services.commits import commit_template
        self.assertEqual(commit_template("git@github.com:o/r.git"),
                         "https://github.com/o/r/commit/{sha}")
        self.assertEqual(commit_template("https://bitbucket.org/o/r"),
                         "https://bitbucket.org/o/r/commits/{sha}")
        self.assertEqual(commit_template("https://x.example.com/c/{sha}"),
                         "https://x.example.com/c/{sha}")
        self.assertIsNone(commit_template("javascript:{sha}"))


@unittest.skipIf(connect is None, "mongoengine not installed")
class TaskCommitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            cls.app.config["COMMIT_URL_TEMPLATES"] = {
                "Mapped": "git@github.com:example-org/mapped.git"}
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
            self.owner = User(username="cm-owner", email="cmo@test.invalid",
                              password_hash="x", level="premium").save()
            self.agent = User(username="cm-agent", email="cma@test.invalid",
                              password_hash="x", level="premium").save()
            self.stranger = User(username="cm-x", email="cmx@test.invalid",
                                 password_hash="x", level="premium").save()
            self.task = Item(owner=self.owner, kind="task", title="Fix sync",
                             assigned_to=self.agent).save()
            self.tok = {u.username: u.issue_api_token("t")[1]
                        for u in (self.owner, self.agent, self.stranger)}
            self.ro = self.owner.issue_api_token("ro", read_only=True)[1]
        self.client = self.app.test_client()

    def _post(self, payload, who="cm-agent", token=None, path="commits"):
        tok = token or self.tok[who]
        return self.client.post(f"/api/v1/tasks/{self.task.pk}/{path}", json=payload,
                                headers={"Authorization": f"Bearer {tok}"})

    def _fresh(self):
        with self.app.app_context():
            return self.Item.objects.get(pk=self.task.pk)

    # -- API -------------------------------------------------------------

    def test_assignee_registers_and_detail_returns_them(self):
        r = self._post({"commits": [{"repo": "mapped", "sha": FULL.upper(),
                                     "subject": "fix: sync"}]})
        self.assertEqual(r.status_code, 201, r.json)
        self.assertEqual(r.json["added"], 1)
        got = self.client.get(f"/api/v1/tasks/{self.task.pk}",
                              headers={"Authorization": f"Bearer {self.tok['cm-owner']}"}).json
        self.assertEqual(len(got["commits"]), 1)
        c = got["commits"][0]
        self.assertEqual(c["sha"], FULL)
        self.assertEqual(c["subject"], "fix: sync")
        self.assertEqual(c["author"]["username"], "cm-agent")
        # Mapping is case-insensitive on the repo name.
        self.assertEqual(c["url"], f"https://github.com/example-org/mapped/commit/{FULL}")

    def test_dedupe_short_and_full(self):
        self._post({"commits": [{"repo": "tool", "sha": FULL[:7]}]})
        r = self._post({"commits": [{"repo": "Tool", "sha": FULL, "subject": "s"},
                                    {"repo": "tool", "sha": FULL[:10]}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["added"], 0)
        commits = self._fresh().commits
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].sha, FULL)   # the longer hash is kept
        self.assertEqual(commits[0].subject, "s")
        # Same sha in another repo is another commit.
        self.assertEqual(self._post({"commits": [{"repo": "other", "sha": FULL}]}).json["added"], 1)

    def test_url_and_remote(self):
        self._post({"commits": [
            {"repo": "a", "sha": "abcdef1", "url": "https://forge.example.com/c/abcdef1"},
            {"repo": "b", "sha": "abcdef2", "remote": "https://u:pw@git.example.com/o/b.git"},
            {"repo": "c", "sha": "abcdef3"},
        ]})
        by_repo = {c["repo"]: c["url"] for c in self.client.get(
            f"/api/v1/tasks/{self.task.pk}",
            headers={"Authorization": f"Bearer {self.tok['cm-agent']}"}).json["commits"]}
        self.assertEqual(by_repo["a"], "https://forge.example.com/c/abcdef1")
        self.assertEqual(by_repo["b"], "https://git.example.com/o/b/commit/abcdef2")
        self.assertIsNone(by_repo["c"])

    def test_bad_input_stores_nothing(self):
        for payload in ({}, {"commits": []}, {"commits": [{"repo": "x", "sha": "nothex!"}]},
                        {"commits": [{"sha": FULL}]},
                        {"commits": [{"repo": "x", "sha": FULL, "url": "javascript:alert(1)"}]},
                        {"commits": [{"repo": "x", "sha": FULL}], "comment_id": "nope"}):
            self.assertEqual(self._post(payload).status_code, 400, payload)
        self.assertEqual(len(self._fresh().commits), 0)

    def test_stranger_gets_404_and_read_only_is_refused(self):
        self.assertEqual(self._post({"commits": [{"repo": "x", "sha": FULL}]},
                                    who="cm-x").status_code, 404)
        self.assertEqual(self._post({"commits": [{"repo": "x", "sha": FULL}]},
                                    token=self.ro).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/v1/tasks/000000000000000000000000/commits",
            json={"commits": [{"repo": "x", "sha": FULL}]},
            headers={"Authorization": f"Bearer {self.tok['cm-agent']}"}).status_code, 404)
        self.assertEqual(len(self._fresh().commits), 0)

    def test_commits_with_a_comment_belong_to_it(self):
        r = self._post({"body": "Done.", "commits": [{"repo": "tool", "sha": FULL}]},
                       path="comments")
        self.assertEqual(r.status_code, 201)
        cid = r.json["comments"][-1]["id"]
        self.assertTrue(cid)
        self.assertEqual(r.json["commits"][0]["comment_id"], cid)
        # A later call can point at that comment too.
        r = self._post({"commits": [{"repo": "tool", "sha": "fedcba9"}], "comment_id": cid})
        self.assertEqual(r.json["commits"][1]["comment_id"], cid)

    def test_refused_commits_leave_no_comment(self):
        r = self._post({"body": "Done.", "commits": [{"repo": "tool", "sha": "zz"}]},
                       path="comments")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(self._fresh().comments), 0)

    def test_compact_row_counts(self):
        self._post({"commits": [{"repo": "tool", "sha": FULL}]})
        rows = self.client.get("/api/v1/tasks?compact=true",
                               headers={"Authorization": f"Bearer {self.tok['cm-owner']}"}).json
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
        row = next(t for t in rows if t["id"] == str(self.task.pk))
        self.assertEqual(row["commit_count"], 1)

    def test_route_is_in_the_spec(self):
        spec = self.client.get("/api/v1/openapi.json").json
        self.assertIn("/tasks/{task_id}/commits", spec["paths"])

    # -- UI --------------------------------------------------------------

    def test_task_page_shows_hashes(self):
        self._post({"body": "Pushed.", "commits": [
            {"repo": "mapped", "sha": FULL, "subject": "fix: the sync"}]}, path="comments")
        self._post({"commits": [{"repo": "local-only", "sha": "abc1234def"}]})
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.owner.pk)
        html = self.client.get(f"/tasks/{self.task.pk}").get_data(as_text=True)
        self.assertIn('id="commits"', html)
        self.assertIn(f'href="https://github.com/example-org/mapped/commit/{FULL}"', html)
        self.assertIn(">01234567</a>", html)
        self.assertIn("fix: the sync", html)
        # No link known: the short hash as text, the full one to copy.
        self.assertIn('<code class="commit-sha">abc1234d</code>', html)
        self.assertIn('data-sha="abc1234def"', html)
        # The chip under the comment it belongs to.
        self.assertIn('class="comment-commits', html)


if __name__ == "__main__":
    unittest.main()
