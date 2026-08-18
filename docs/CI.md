# Running the suite from GitHub Actions

Two manual workflows:

| Workflow | What it does |
|---|---|
| **Run Salesforce Tests** | Runs the tests you pick, publishes the report to GitHub Pages, emails it |
| **Clean up published reports** | Deletes old published runs on a retention window you choose |
| **Sync test dropdown** | Keeps the test picker in step with the files on disk (automatic) |

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
| `MAILJET_API_KEY` | your Mailjet **API Key** | Mailjet → Account Settings → REST API keys |
| `MAILJET_SECRET_KEY` | your Mailjet **Secret Key** | same page, shown next to the API key |

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
| `MAIL_FROM` | the sender address, e.g. `ashish5social@gmail.com` — **must be validated in Mailjet** |
| `SFAUTO_PROFILE` | `default` *(optional)* |
| `MAIL_FROM_NAME` | `sfauto CI` *(optional — this is the default)* |
| `MAIL_SERVER` | `in-v3.mailjet.com` *(optional — this is the default)* |
| `MAIL_PORT` | `587` *(optional — this is the default)* |
| `MAIL_SECURE` | `false` *(optional — `false` = STARTTLS on 587; set `true` with port `465` for SMTPS)* |

### 3. Mailjet

Mailjet is used through its **SMTP relay**, so there is no extra library
or API client involved — the API Key is the SMTP username and the Secret
Key is the password. Mailjet issues no separate SMTP credential.

1. Sign in → **Account Settings → REST API keys (SMTP and SEND API settings)**
2. Copy the **API Key** into `MAILJET_API_KEY` and the **Secret Key**
   into `MAILJET_SECRET_KEY`
3. **Validate your sender**: Account Settings → **Add a sender address**
   (or authenticate a whole domain). Put that exact address in the
   `MAIL_FROM` variable.

> Mailjet rejects any message whose `From` is not a validated sender.
> This is the most common cause of a failing email step — the SMTP login
> succeeds and the send is refused afterwards.

Connection defaults: `in-v3.mailjet.com` port `587` with STARTTLS. To use
implicit TLS instead, set `MAIL_PORT=465` and `MAIL_SECURE=true`.

Switching provider later means changing only the four `MAIL_*` variables
and the two secrets — nothing in the workflow logic is Mailjet-specific.

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
> screenshot and video of your org. Since the report is delivered as a
> link rather than an attachment, the published page *is* the delivery
> mechanism: anyone with the URL can read it. If that is not what you
> want, make the repository private (Pages then needs a paid plan) or
> drop the publish step and take the report from the workflow artifact,
> which is still uploaded on every run.

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

- an **email** with the pass/fail summary and a link to the full report
- the full report **published** to Pages
- the same HTML as a **workflow artifact** (14-day retention)
- a **job summary** on the run page

The report is **linked, never attached**. It embeds video and would
outgrow any mail provider's size limit as the suite grows (Mailjet caps a
message at 15 MB, and base64 encoding adds about a third on top). The
email stays small and fast; the report stays complete.

If publishing fails for any reason, the email still goes out and points
at the workflow run, where the same report is available as an artifact.

The job fails if any test failed — but the email is sent either way, so a
red run still tells you what broke.

### Adding a new test to the dropdown

Nothing to do. `workflow_dispatch` choice options are static YAML, so the
**Sync test dropdown** workflow regenerates them on every push that adds
or removes a `tests/{ui,api}/test_*.py` file and commits the result.

To refresh it by hand:

```bash
python scripts/sync_test_dropdown.py          # rewrite the options block
python scripts/sync_test_dropdown.py --check  # exit 1 if out of sync
```

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

This prunes *published reports*. Test records left in Salesforce are a
separate concern — the suite deletes what it creates at teardown (see
`SFAUTO_KEEP_RECORDS`), and `scripts/cleanup_test_data.py` sweeps up
anything older that still carries the record prefix:

```bash
python scripts/cleanup_test_data.py --keep-days 3 --dry-run
```

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

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no usable credential` in doctor | `SF_USERNAME` / `SF_CLIENT_ID` / `SF_JWT_KEY` secret missing or empty |
| `openssl rsa ... unable to load` | `SF_JWT_KEY` is truncated — it must include the BEGIN/END lines |
| `user hasn't approved this consumer` | The app isn't pre-authorized for the user's profile — see [AUTHENTICATION.md](AUTHENTICATION.md) |
| Email step fails with `535` | Wrong `MAILJET_API_KEY` / `MAILJET_SECRET_KEY` |
| Email rejected after login succeeds | `MAIL_FROM` is not a validated Mailjet sender |
| Job fails at *Check mail configuration* | One of the two secrets or `MAIL_FROM` is unset |
| Pages URL 404s | Pages isn't enabled yet, or is pointed at the wrong branch (must be `gh-pages` / root) |
| `Permission denied` pushing gh-pages | Workflow lacks `permissions: contents: write` |
