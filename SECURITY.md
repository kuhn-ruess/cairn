# Security policy

## Reporting a vulnerability

Please report security problems **privately** — not in a public issue, not in a
pull request, and not on the tracker.

Write to **mail@bastian-kuhn.de**, the address the maintainers read. Put "Cairn security" in the subject so it is not read as ordinary
mail.

Helpful in a report:

* what the flaw lets an attacker do, in one sentence;
* the steps to reproduce it, or a request/response pair;
* the version — the About page prints it, and `/api/v1/openapi.json` carries
  the same number;
* whether you are looking at a self-hosted instance or at the hosted one
  (`app.cairn-app.de`).

You can expect an acknowledgement, a fix on `main` once the problem is
understood, and credit in the commit if you want it. Please give us a chance to
ship the fix before writing about the issue publicly.

## Scope

Cairn is maintained as a rolling release: there are no release branches and no
backports, so **the fix lands on `main`** and a self-hosted deployment picks it
up by updating. Only the current `main` is supported.

What is in scope is the code in this repository and the hosted instance at
`app.cairn-app.de`. Findings against a third-party service Cairn talks to
(Zammad, Google, Microsoft Graph, a CalDAV server, Stripe) belong with that
vendor.

Two things are deliberate rather than bugs, so a report about them will be
closed as such: the public project share (`/p/<token>`) and the public booking
page (`/book/<slug>`) answer anonymous requests by design — the token *is* the
authentication there, and anyone holding the link is meant to see the page.
