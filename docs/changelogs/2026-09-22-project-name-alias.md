# Calliope 1.5.6 — patch: `name` is accepted for a project's title

A field-name mismatch could kill a storyline build at step zero. LLM agents —
and anyone scripting against the API — very often emit `{"name": "My Film"}`
for the project title, but every surface required the key to be exactly
`title`:

- `POST /api/projects` / `PATCH /api/projects/{id}` rejected `name` with an
  opaque **422 validation error**.
- The agent's `create_project` tool returned
  `"title is required (non-empty string)"` and the turn dead-ended — no project
  row was ever created, which downstream looks exactly like *"the agent said it
  wrote a story but nothing appeared in the project."*

The Calliope UI never hit this (the form always sends `title`), which is why it
only surfaced in agent chats and external/API use.

## What changed

- **REST schema accepts either key.** `ProjectCreate` and `ProjectUpdate`
  validate `title` via `AliasChoices("title", "name")` with
  `populate_by_name=True` — `name` payloads now succeed and still store into
  the `title` column. No behavior change for existing `title` callers.
- **Agent tool falls back to `name`.** `create_project` / `update_project` use
  `args["name"]` when `title` is absent or blank, mirroring the REST alias.
  A call that previously failed now creates the project and auto-links the
  session as usual. The null/empty-title guard is unchanged — a genuinely
  missing title still errors instead of creating a "None" project.

## For existing users

No data repair is needed — the failure happened *before* any insert, so nothing
was half-written or misnamed. After updating, repeat the same request in the
affected session and the build proceeds normally.

## Testing

- `tests/test_projects.py` — create/patch with `name` end-to-end.
- `tests/test_agent_security.py` — the tool accepts `name`; the null-title and
  200-char bound guards still hold.
