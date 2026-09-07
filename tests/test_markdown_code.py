"""Code in a note body has to render as what was typed.

Run with ``python -m unittest discover tests``. No database — the filter is
registered on a bare Flask app.

``bleach.linkify`` takes a ``skip_tags`` list, and the name is a lie: it does
not skip the text, it re-serializes it and escapes it a second time on the way
out. A code block containing ``<<<x>>>`` therefore reached the page as
``&lt;&lt;&lt;x&gt;&gt;&gt;`` — the entities printed literally, which reads as
the editor having mangled the snippet. Dropping ``skip_tags`` fixes the
escaping and breaks the other half: every URL inside a code sample turns into
a link.

Both halves are tested here, because a fix for either one alone is a
regression for the other.
"""
import unittest

try:
    from flask import Flask
    from application.views.filters import register_filters
except ImportError:  # pragma: no cover - dependency missing
    Flask = None


def render(md):
    app = Flask(__name__)
    register_filters(app)
    return str(app.jinja_env.filters["markdown"](md))


@unittest.skipIf(Flask is None, "flask/bleach not installed")
class MarkdownCodeTest(unittest.TestCase):
    def test_angle_brackets_in_a_fence_are_not_escaped_twice(self):
        out = render("```\n<<<digital_signage:sep(124):cached(1788785557,120)>>>\n```")
        self.assertIn("&lt;&lt;&lt;digital_signage:sep(124):cached(1788785557,120)&gt;&gt;&gt;", out)
        self.assertNotIn("&amp;lt;", out)
        self.assertNotIn("&amp;gt;", out)

    def test_angle_brackets_in_an_inline_span_are_not_escaped_twice(self):
        out = render("Siehe `<b>fett</b>` im Text")
        self.assertIn("<code>&lt;b&gt;fett&lt;/b&gt;</code>", out)
        self.assertNotIn("&amp;lt;", out)

    def test_a_url_inside_code_stays_literal(self):
        out = render("```\ncurl https://example.com/a?x=1\n```")
        self.assertNotIn("<a ", out)
        self.assertIn("curl https://example.com/a?x=1", out)

    def test_a_bare_url_outside_code_still_becomes_a_link(self):
        out = render("Siehe https://example.com/x dazu")
        self.assertIn('href="https://example.com/x"', out)

    def test_code_and_prose_in_one_body(self):
        out = render(
            "Vorher https://example.com/a\n\n"
            "```\n<<<x>>> https://example.com/b\n```\n\n"
            "Nachher https://example.com/c"
        )
        self.assertIn('href="https://example.com/a"', out)
        self.assertIn('href="https://example.com/c"', out)
        self.assertIn("&lt;&lt;&lt;x&gt;&gt;&gt; https://example.com/b", out)
        self.assertNotIn('href="https://example.com/b"', out)
        self.assertNotIn("&amp;lt;", out)

    def test_ampersand_in_prose_is_escaped_once(self):
        out = render("A & B, 5 < 7")
        self.assertIn("A &amp; B, 5 &lt; 7", out)
        self.assertNotIn("&amp;amp;", out)


if __name__ == "__main__":
    unittest.main()
