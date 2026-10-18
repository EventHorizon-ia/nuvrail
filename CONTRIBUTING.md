# Contributing to Nuvrail

Thanks for your interest in improving Nuvrail — the IMAP/SMTP approval gateway
that lets you give an AI agent email access without giving it your mailbox.

Nuvrail is a security tool that handles other people's credentials and email.
We hold contributions to that bar: **correctness, tests, and no secrets in the
tree** are non-negotiable. This guide gets you from clone to a green PR.

## Ways to contribute

- **Report a bug** — open an issue with the bug template. Include your provider
  (your provider, as in the bug template's dropdown), Nuvrail version or commit, and repro steps.
- **Request a feature** — open an issue with the feature template and describe
  the problem before the solution.
- **Report a security vulnerability** — **do not** open a public issue. Follow
  [SECURITY.md](SECURITY.md) (GitHub private advisory or `security@agensfield.com`).
- **Send a pull request** — see below.

## Development setup

Nuvrail is a Python gateway (the proxy + API) plus a React web app (the PWA).

### Gateway / API (Python)

Requires **Python 3.9+** (CI runs on 3.11).

```bash
# from the repo root
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"          # installs pytest, pytest-asyncio, httpx, ruff
```

### Web app (PWA)

Requires **Node 18+**.

```bash
cd web
npm install
```

### Running it locally

The fastest full-stack path is the self-host Docker Compose flow — see the
[README](README.md) quickstart. For the gateway alone, copy the example env and
run under uvicorn:

```bash
cp .env.example .env             # then fill in the required values
# see README for the run command and required NUVRAIL_* env vars
```

## Before you open a PR

Run the same checks CI runs — a PR that's green locally is green in CI.

### 1. Lint (ruff)

```bash
python -m ruff check .
```

Line length is 100 (configured in `pyproject.toml`). Fix lint before pushing.

### 2. Tests (pytest / pytest-asyncio)

```bash
python -m pytest --ignore=tests/integration --ignore=tests/e2e --tb=short -q
```

`asyncio_mode = auto` is set, so `async def test_...` functions run without a
decorator. The `integration/` and `e2e/` suites need real email credentials and
are **not** run in CI — don't rely on them for PR validation.

**Tests are part of the change, not an afterthought.** New behavior needs new
tests; a bug fix needs a regression test that fails before the fix and passes
after. PRs that lower coverage of a security-relevant path (the approval
boundary, credential handling, per-agent isolation) will be asked for tests.

### 3. Web build (if you touched `web/`)

```bash
cd web
npm run type-check     # tsc --noEmit
npm run build          # tsc && vite build
```

### 4. Secret scan (gitleaks)

Never commit credentials, `.env` files, `master.key`, or OAuth client secrets.
A pre-push hook runs gitleaks automatically; you can also run it by hand:

```bash
gitleaks detect --source . --no-git --config .gitleaks.toml
```

If you hit a false positive, add an allowlist entry to `.gitleaks.toml` in the
same PR.

### 5. Docs & diagrams

If you changed behavior, update the docs in the **same PR** — we don't merge
undocumented behavior changes. If you touched code near an ASCII diagram or a
doc that describes the flow you changed, update it too; a stale diagram is worse
than none.

## Pull request expectations

- **One logical change per PR.** Small, reviewable diffs merge faster.
- **Describe the why**, not just the what. Link the issue it closes
  (`Closes #123`).
- **Fill in the PR checklist** (tests, gitleaks clean, docs/diagram updates).
- **CI must be green** — lint, tests, and the self-host `docker compose build`
  all run on every PR.
- A maintainer reviews every PR before merge. Expect review comments; that's
  the process working, not a rejection.

## License and sign-off (DCO)

Nuvrail core is licensed under **AGPL-3.0-only** (see [LICENSE](LICENSE)). By
contributing, you agree that your contribution is licensed under AGPL-3.0.

We use the **Developer Certificate of Origin** ([DCO](https://developercertificate.org/)):
you certify you have the right to submit the code under the project's license.
Sign off every commit with:

```bash
git commit -s -m "your message"
```

which appends a `Signed-off-by: Your Name <you@example.com>` line. Commits
without a sign-off will be asked to add one before merge.

## Code of Conduct

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). By participating,
you're expected to uphold it. Report unacceptable behavior to the contact listed
there.

---

Questions that aren't a bug or feature request? Open a
[discussion or issue](https://github.com/agens-field/nuvrail/issues) and we'll
help.
