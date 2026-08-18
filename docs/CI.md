# Running the suite from GitHub Actions

Two manual workflows:

| Workflow | What it does |
|---|---|
| **Run Salesforce Tests** | Runs the tests you pick, publishes the report to GitHub Pages, emails it |
| **Clean up published reports** | Deletes old published runs on a retention window you choose |

Both are `workflow_dispatch` — you start them from the **Actions** tab, no
push required.

---

## One-time setup

### 1. Repository secrets

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

| Secret | Value | Where it comes from |
|---|---|---|
| `SF_USERNAME` | `you@example.com` | your Salesforce login |
| `SF_CLIENT_ID` | `3MVG9...` | External Client App → consumer key |
| `SF_JWT_USERNAME` | `you@example.com` | the user JWT impersonates (usually the same) |
| `SF_JWT_KEY` | the **entire** contents of `.certs/sfauto.key` | generated during JWT setup |
| `MAIL_USERNAME` | `you@gmail.com` | the account that sends the report |
| `MAIL_PASSWORD` | Gmail **App Password** (16 chars) | see below |

For `SF_JWT_KEY`, paste the whole file including both delimiter lines:

```bash
pbcopy < .certs/sfauto.key
```

```
-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBg...
-----END PRIVATE KEY-----
```

> Never commit the key itself — `.certs/` is git-ignored. The workflow
> writes it to disk at run time and it disappears with the runner.

### 2. Repository variables

Same page, **Variables** tab. These are not secret, so they stay readable:

| Variable | Value |
|---|---|
| `SF_LOGIN_URL` | `https://orgfarm-936e70817d.my.salesforce.com` |
| `SFAUTO_PROFILE` | `default` *(optional)* |
| `MAIL_SERVER` | `smtp.gmail.com` *(optional — this is the default)* |
| `MAIL_PORT` | `465` *(optional — this is the default)* |

### 3. Gmail App Password

Gmail rejects your normal password over SMTP. You need an App Password,
which requires 2-Step Verification to be on:

1. https://myaccount.google.com/security → turn on **2-Step Verification**
2. https://myaccount.google.com/apppasswords
3. Name it `sfauto CI` → **Create**
4. Copy the 16-character code into the `MAIL_PASSWORD` secret

Using a different provider? Set `MAIL_SERVER` / `MAIL_PORT` variables
(Outlook: `smtp.office365.com` / `587`).

### 4. Enable GitHub Pages

The first test run creates the `gh-pages` branch. After that run:

**Settings → Pages → Source: Deploy from a branch → Branch: `gh-pages` / `root` → Save**

Reports then appear at:

```
https://<owner>.github.io/<repo>/                      index of all runs
https://<owner>.github.io/<repo>/runs/<date>_<id>/     one run
```

> **This site is public.** On a public repository, GitHub Pages is
> readable by anyone and indexed by search engines — including every
> screenshot and video of your org. If that is not what you want, remove
> the "Publish report to gh-pages" step and rely on the emailed
> attachment, which stays private.

---

## Running tests

**Actions → Run Salesforce Tests → Run workflow**

| Input | Meaning |
|---|---|
| **tests** | `All tests`, `All UI tests`, `All API tests`, a specific file, or `Custom` |
| **custom_target** | Only read when tests = `Custom`. A path (`tests/ui`) or a `-k` expression (`account and not delete`) |
| **emails** | Comma-separated recipients: `a@x.com, b@y.com` |
| **workers** | Parallel pytest workers (1 / 2 / 4) |

What you get:

- an **email** with a pass/fail summary table, a link to the published
  report, and the full self-contained HTML attached
- the same HTML **published** to Pages
- the same HTML as a **workflow artifact** (14-day retention)
- a **job summary** on the run page

The job fails if any test failed — but the email is sent either way, so a
red run still tells you what broke.

### Adding a new test to the dropdown

`workflow_dispatch` choice options are static YAML. When you add a test
file, add a line to `options:` in
`.github/workflows/run-tests.yml`. Until you do, `All tests` still picks
it up automatically, and `Custom` can target it directly.

---

## Cleaning up

**Actions → Clean up published reports → Run workflow**

Retention counts **today as day 1**:

| `keep_days` | Effect |
|---|---|
| `0` | delete everything, including today's runs |
| `1` | keep today only |
| `2` | keep today and yesterday |
| `7` | keep today and the previous six days |

- **dry_run** (default **on**) lists what would go without deleting.
  Run it once that way first.
- **purge_artifacts** also deletes workflow artifacts older than the same
  window.

"Today" is the **org's** local date from the active profile, not the
runner's UTC date. GitHub runners are UTC, and for IST that is a
different calendar day for five and a half hours every night — so a run
made at 9pm IST would look like yesterday to a UTC runner and get
deleted a day early.

---

## Why reports are stored the way they are

Each run publishes one file: `runs/<date>_<run_id>/index.html`. It has
every screenshot, video and API request/response embedded as base64, so
there is nothing to break and nothing to fetch — you can save it, mail
it, or open it offline years later.

That also means the raw `screenshots/` and `videos_tmp/` folders are
*not* published separately: the media is already inside the HTML, and
publishing it twice would double the size of the branch for no gain.

The `gh-pages` branch grows by roughly the size of one report per run
(~1.7 MB for two tests with video, more as the suite grows). That is what
the cleanup workflow is for — run it on a schedule you're comfortable
with, or add a `schedule:` trigger to automate it.

If a report exceeds `SFAUTO_MAX_ATTACH_MB` (default 20 MB) it is not
attached to the email, because mail servers reject oversized messages —
Gmail's limit is 25 MB. The published link in the body still has
everything.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no usable credential` in doctor | `SF_USERNAME` / `SF_CLIENT_ID` / `SF_JWT_KEY` secret missing or empty |
| `openssl rsa ... unable to load` | `SF_JWT_KEY` is truncated — it must include the BEGIN/END lines |
| `user hasn't approved this consumer` | The app isn't pre-authorized for the user's profile — see [AUTHENTICATION.md](AUTHENTICATION.md) |
| Email step fails with `535` | Using your Gmail password instead of an App Password |
| Pages URL 404s | Pages isn't enabled yet, or is pointed at the wrong branch (must be `gh-pages` / root) |
| `Permission denied` pushing gh-pages | Workflow lacks `permissions: contents: write` |
