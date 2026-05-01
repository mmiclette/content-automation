# NeuroFlow Content Automation

Agentic pipeline for generating NotebookLM Cinematic videos and NeuroFlow journey spreadsheets, orchestrated through Slack slash commands and GitHub Actions.

---

## How it works

```
/video [topic]: [context]    →  GitHub Actions  →  Claude research  →  NotebookLM  →  Slack notification
/journey [topic]             →  GitHub Actions  →  Claude journey   →  Slack spreadsheet  →  Video batches (2 at a time)
```

**Key design decisions:**
- All secrets live in GitHub Secrets — nothing is hardcoded.
- NotebookLM has no API. A Playwright browser agent handles all UI interactions.
- The webhook relay is a small Flask server (free tier on Render) that receives Slack slash commands and dispatches GitHub Actions workflows.
- Journey videos generate in batches of 2. Each batch waits for human review approval in Slack before the next batch button appears.

---

## Repository structure

```
content-automation/
├── .github/workflows/
│   ├── video_creation.yml      # Triggered by /video slash command
│   ├── journey_creation.yml    # Triggered by /journey slash command
│   └── video_batch.yml         # Triggered by relay for each journey batch
├── agents/
│   ├── research_agent.py       # Claude API: generates NotebookLM source doc + steering prompt
│   ├── notebooklm_agent.py     # Playwright: creates notebook, configures video, polls for completion
│   ├── journey_builder.py      # Claude API: generates journey spreadsheet (xlsx)
│   └── slack_notifier.py       # All outbound Slack messages
├── prompts/
│   ├── video_research.txt      # Evidence synthesis prompt for videos
│   └── journey_research.txt    # Journey development prompt
├── webhook_relay/
│   ├── app.py                  # Flask server: receives Slack events, dispatches workflows
│   ├── state_store.py          # Thread-safe JSON state for journey batch tracking
│   └── requirements.txt
├── scripts/
│   └── export_session.py       # ONE-TIME: captures your Google session for NotebookLM
├── requirements.txt
└── .env.example
```

---

## Setup — Step by step

### 1. Clone the repo

```bash
git clone https://github.com/mmiclette/content-automation.git
cd content-automation
```

### 2. Export your NotebookLM session (run locally, once)

This step captures your Google authentication token so the Playwright agent can access NotebookLM without a login prompt.

```bash
pip install playwright
playwright install chromium
python scripts/export_session.py
```

A browser window will open. Log in as `matthew@neuroflow.com`, complete any 2FA, and press ENTER in the terminal when you land on NotebookLM. The script writes `notebooklm_session.txt`.

**Copy the full contents of `notebooklm_session.txt` — you will need it in the next step. Delete the file immediately afterward.**

```bash
cat notebooklm_session.txt   # copy this output
rm notebooklm_session.txt
```

Re-run this script if you ever get authentication errors in the pipeline.

### 3. Add GitHub Secrets

Go to `https://github.com/mmiclette/content-automation/settings/secrets/actions` and add each of the following:

| Secret name | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `SLACK_BOT_TOKEN` | Slack App → OAuth & Permissions → Bot Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Slack App → Basic Information → Signing Secret |
| `SLACK_CHANNEL_ID` | Right-click `#content-automation` in Slack → Copy channel ID (`C...`) |
| `NOTEBOOKLM_SESSION` | Output of `export_session.py` (base64 string) |
| `NOTEBOOKLM_EMAIL` | `matthew@neuroflow.com` |
| `GH_PAT` | github.com/settings/tokens → New token (classic) with `repo` + `workflow` scopes |
| `GH_REPO` | `mmiclette/content-automation` |
| `RELAY_URL` | Your Render/Railway deployment URL (set this after step 5) |
| `RELAY_SECRET` | Any random string — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |

### 4. Create your Slack App

Go to `https://api.slack.com/apps` → Create New App → From scratch.

**OAuth & Permissions — Bot Token Scopes:**
- `chat:write`
- `commands`
- `files:write`

**Slash Commands — create two:**

| Command | Request URL | Description |
|---|---|---|
| `/video` | `https://your-relay.onrender.com/slack/video` | Generate a video |
| `/journey` | `https://your-relay.onrender.com/slack/journey` | Generate a journey |
| `/rerun` | `https://your-relay.onrender.com/slack/rerun` | Rerun a video with same or revised inputs |

**Interactivity & Shortcuts:**
- Enable Interactivity
- Request URL: `https://your-relay.onrender.com/slack/actions`

Install the app to your workspace. Add the bot to `#content-automation`.

### 5. Deploy the webhook relay

**Using Render (recommended — free tier works):**

1. Go to `https://render.com` → New → Web Service
2. Connect your GitHub repo
3. Set the root directory to `webhook_relay`
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
6. Add all environment variables from `.env.example` under the relay section

After deploy, copy the service URL (e.g., `https://content-automation-relay.onrender.com`) and:
- Update the `RELAY_URL` GitHub Secret with this URL
- Update the `/video` and `/journey` slash command Request URLs in your Slack App with the matching relay endpoints

**Test the relay is running:**
```bash
curl https://your-relay.onrender.com/health
# Expected: {"status": "ok"}
```

---

## Using the pipeline

### Single video

```
/video Understanding Depression
/video Veterans and Sleep Disorders: Focus on insomnia treatment approaches at the VA
```

Format: `/video [topic]: [optional context for more specificity]`

You will receive a Slack acknowledgment immediately. The video typically takes 45–75 minutes. When it is ready, a Slack message will appear with an "Open in NotebookLM" link and Approve / Flag for revision buttons.

### Journey

```
/journey Depression Management Program
/journey Anxiety and Stress Reduction for Veterans
```

### Rerun a video

Use the **Rerun video** button on any video ready notification to regenerate that video immediately with the same topic and context. No typing required.

To rerun with revised inputs, use the slash command:

```
/rerun Understanding Depression
/rerun Veterans and Sleep Disorders: Narrow focus to insomnia only, exclude sleep apnea
```

Format: `/rerun [topic]: [optional revised context]`

When a rerun is triggered from a journey batch notification, the relay resets that video's approval status to pending so the batch completion gate waits for the new version before posting the "Start Next Batch" button.

The pipeline will:
1. Generate the journey spreadsheet and post it to Slack for review
2. Automatically start the first batch of 2 videos
3. When both videos are approved, post a "Start Batch 2" button
4. Continue until all videos are complete
5. Post a final "Journey complete" notification

---

## Batch management

Each journey notification includes the batch progress (e.g., "Batch 1 of 4 complete"). Clicking "Start Batch 2" dispatches the next two videos. You can hold batches indefinitely — the button stays in the Slack message until clicked.

If a video is flagged for revision, it does not count as approved and will not trigger the next batch button. Fix the video in NotebookLM manually, then click Approve on the original Slack message.

---

## Troubleshooting

**NotebookLM authentication errors**
Re-run `scripts/export_session.py` and update the `NOTEBOOKLM_SESSION` GitHub Secret.

**"Could not locate visual style input" in logs**
NotebookLM's UI changed. The agent falls back gracefully and continues. File a GitHub Issue with a screenshot of the current NotebookLM video panel so the selectors can be updated.

**Polling timed out after 2 hours**
NotebookLM did not complete generation. The Slack error notification includes the notebook URL. Open it manually to check status. The 2-hour ceiling can be raised by changing `MAX_WAIT` in `agents/notebooklm_agent.py`.

**Relay not responding**
Check the Render deployment logs. The relay logs all incoming requests. Free tier services on Render spin down after inactivity — the first request after a cold start takes 30–60 seconds. If you use a slash command and Slack shows a timeout error, try the command again immediately.

**Journey state lost after relay restart**
The relay persists `state.json` on disk. Render's free tier uses ephemeral storage — if the service restarts, in-progress journey state is lost. Upgrade to Render's Starter plan ($7/mo) to use a persistent disk, or migrate state to a free Supabase or PlanetScale database.

---

## GitHub Actions minute usage

| Workflow | Estimated duration | Actions minutes |
|---|---|---|
| `video_creation.yml` | 60–90 min | 60–90 min |
| `journey_creation.yml` | 5–10 min | 5–10 min |
| `video_batch.yml` (2 videos) | 60–90 min | 120–180 min (parallel) |

GitHub's free tier includes 2,000 minutes/month for private repos. A 12-video journey uses approximately 720 minutes. Plan accordingly or upgrade to a paid Actions plan if volume increases.

---

## Secrets summary (quick reference)

```
ANTHROPIC_API_KEY       Claude API access
SLACK_BOT_TOKEN         Slack bot authentication
SLACK_SIGNING_SECRET    Verifies requests are from Slack
SLACK_CHANNEL_ID        Target channel for all notifications
NOTEBOOKLM_SESSION      Base64 Google session (from export_session.py)
NOTEBOOKLM_EMAIL        matthew@neuroflow.com
GH_PAT                  Allows relay to dispatch GitHub Actions workflows
GH_REPO                 mmiclette/content-automation
RELAY_URL               Public URL of your Render/Railway relay service
RELAY_SECRET            Shared secret between relay and GitHub Actions
```
