"""
app.py — Webhook relay server

Receives Slack slash commands and interactive button actions,
then dispatches GitHub Actions workflows via the GitHub API.

Deploy this to Render or Railway (free tier is sufficient).
Set all environment variables listed in .env.example.

Endpoints:
  POST /slack/video    — handles /video slash command
  POST /slack/journey  — handles /journey slash command
  POST /slack/actions  — handles all Slack button interactions
  POST /journey/register — called by the journey_creation workflow to register state
"""

import os
import json
import hmac
import hashlib
import time
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from state_store import (
    create_journey,
    get_journey,
    get_batch_topics,
    update_video_status,
    reset_video_status,
    is_batch_complete,
    is_journey_complete,
    advance_batch,
)

load_dotenv()

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID     = os.environ["SLACK_CHANNEL_ID"]
GH_PAT               = os.environ["GH_PAT"]
GH_REPO              = os.environ["GH_REPO"]          # e.g. "mmiclette/content-automation"
RELAY_SECRET         = os.environ.get("RELAY_SECRET", "")
GH_API               = "https://api.github.com"
SLACK_API            = "https://slack.com/api"


# ─── Slack signature verification ────────────────────────────────────────────

def verify_slack(req) -> bool:
    ts  = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")

    if not ts or not sig:
        return False
    if abs(time.time() - float(ts)) > 300:
        return False   # replay attack guard

    body = req.get_data(as_text=True)
    base = f"v0:{ts}:{body}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        base.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, sig)


# ─── GitHub Actions dispatch ──────────────────────────────────────────────────

def dispatch(workflow_file: str, inputs: dict):
    url = f"{GH_API}/repos/{GH_REPO}/actions/workflows/{workflow_file}/dispatches"
    headers = {
        "Authorization":        f"Bearer {GH_PAT}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    resp = requests.post(url, headers=headers, json={"ref": "main", "inputs": inputs}, timeout=15)
    resp.raise_for_status()


# ─── Slack messaging ──────────────────────────────────────────────────────────

def slack_post(blocks: list, text: str):
    requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type":  "application/json"
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "blocks":  blocks,
            "text":    text
        },
        timeout=10
    )


def post_batch_complete(journey_id: str, journey_topic: str, batch_num: int, total_batches: int, completed_topics: list):
    is_last = batch_num >= total_batches
    topic_list = "\n".join(f"  • {t}" for t in completed_topics)

    if is_last:
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": ":tada:  Journey complete — all videos approved"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{journey_topic}*\nAll {len(completed_topics)} videos have been reviewed and approved.\n\n{topic_list}"}}
        ]
        slack_post(blocks, f"Journey complete: {journey_topic}")
    else:
        next_num = batch_num + 1
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f":white_check_mark:  Batch {batch_num} of {total_batches} complete"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{journey_topic}*\nApproved videos:\n{topic_list}"}},
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [{
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": f"Start Batch {next_num} of {total_batches}  \u25b6"},
                    "style":     "primary",
                    "action_id": "start_batch",
                    "value":     json.dumps({"journey_id": journey_id, "batch_num": next_num})
                }]
            }
        ]
        slack_post(blocks, f"Batch {batch_num}/{total_batches} complete for {journey_topic}")


def post_revision_flagged(topic: str, journey_id: str = ""):
    context = f" (journey {journey_id})" if journey_id else ""
    slack_post(
        [{"type": "section", "text": {"type": "mrkdwn", "text": f":pencil2: *Revision flagged*: {topic}{context}\nThis video has been marked for revision and will not block the next batch."}}],
        f"Revision flagged: {topic}"
    )


# ─── Slash command handlers ───────────────────────────────────────────────────

@app.post("/slack/video")
def handle_video():
    if not verify_slack(request):
        return jsonify({"error": "Invalid signature"}), 401

    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"response_type": "ephemeral", "text": "Usage: `/video [topic]: [optional context]`"})

    if ":" in text:
        topic, context = [p.strip() for p in text.split(":", 1)]
    else:
        topic, context = text, ""

    # Dispatch in background so Slack gets a response within 3 seconds
    def _dispatch():
        try:
            dispatch("video_creation.yml", {"topic": topic, "context": context})
        except Exception as e:
            slack_post(
                [{"type": "section", "text": {"type": "mrkdwn",
                  "text": ":x: *Video workflow failed to start*\n*Topic:* " + topic + "\n*Error:* " + str(e)}}],
                "Video workflow failed to start: " + topic
            )

    import threading
    threading.Thread(target=_dispatch, daemon=True).start()

    return jsonify({
        "response_type": "in_channel",
        "text": f":clapper: Video workflow started for *{topic}*. I'll post here when the video is ready for review."
    })


@app.post("/slack/journey")
def handle_journey():
    if not verify_slack(request):
        return jsonify({"error": "Invalid signature"}), 401

    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"response_type": "ephemeral", "text": "Usage: `/journey [topic]: [optional context]`"})

    if ":" in text:
        topic, context = [p.strip() for p in text.split(":", 1)]
    else:
        topic, context = text, ""

    context_note = f" — context: _{context}_" if context else ""

    def _dispatch_journey():
        try:
            dispatch("journey_creation.yml", {"topic": topic, "context": context})
        except Exception as e:
            slack_post(
                [{"type": "section", "text": {"type": "mrkdwn",
                  "text": ":x: *Journey workflow failed to start*\n*Topic:* " + topic + "\n*Error:* " + str(e)}}],
                "Journey workflow failed to start: " + topic
            )

    import threading
    threading.Thread(target=_dispatch_journey, daemon=True).start()

    return jsonify({
        "response_type": "in_channel",
        "text": f":scroll: Journey workflow started for *{topic}*{context_note}. I'll post the spreadsheet here once it's generated."
    })


@app.post("/slack/rerun")
def handle_rerun():
    if not verify_slack(request):
        return jsonify({"error": "Invalid signature"}), 401

    text = request.form.get("text", "").strip()
    if not text:
        return jsonify({"response_type": "ephemeral", "text": "Usage: `/rerun [topic]: [optional context]`"})

    if ":" in text:
        topic, context = [p.strip() for p in text.split(":", 1)]
    else:
        topic, context = text, ""

    context_note = f" — context: _{context}_" if context else ""

    def _dispatch_rerun():
        try:
            dispatch("video_creation.yml", {"topic": topic, "context": context})
        except Exception as e:
            slack_post(
                [{"type": "section", "text": {"type": "mrkdwn",
                  "text": ":x: *Rerun failed to start*\n*Topic:* " + topic + "\n*Error:* " + str(e)}}],
                "Rerun failed to start: " + topic
            )

    import threading
    threading.Thread(target=_dispatch_rerun, daemon=True).start()

    return jsonify({
        "response_type": "in_channel",
        "text": f":repeat: Rerunning video for *{topic}*{context_note}. I'll post here when the new version is ready."
    })


@app.post("/slack/refresh-session")
def handle_refresh_session():
    if not verify_slack(request):
        return jsonify({"error": "Invalid signature"}), 401

    # Post the exact commands to Slack so the user never has to remember them
    slack_post([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":key:  How to refresh the NotebookLM session"}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Step 1 — Run this in Terminal:*\n"
                    "```cd ~/content-automation && python3 scripts/export_session.py```\n\n"
                    "A browser window will open. Log in with your Google email and password. "
                    "Once NotebookLM loads, press Enter in Terminal.\n\n"
                    "*Step 2 — Update GitHub Secret:*\n"
                    "Open `notebooklm_session.txt` in TextEdit, copy all the text, then go to:\n"
                    "```https://github.com/mmiclette/content-automation/settings/secrets/actions```\n"
                    "Update `NOTEBOOKLM_SESSION` with the copied value.\n\n"
                    "*Step 3 — Delete the local file:*\n"
                    "```rm ~/content-automation/notebooklm_session.txt```\n\n"
                    "Then rerun your video with `/rerun [topic]`"
                )
            }
        }
    ], "How to refresh the NotebookLM session")

    return jsonify({
        "response_type": "ephemeral",
        "text": "Instructions posted to #content-automation."
    })


# ─── Journey registration (called by journey_creation.yml) ───────────────────

@app.post("/journey/register")
def register_journey():
    """
    Called by the journey_creation GitHub Actions workflow after the
    spreadsheet is posted to Slack. Stores journey state and dispatches batch 1.
    """
    if request.headers.get("X-Relay-Secret", "") != RELAY_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data       = request.get_json()
    journey_id = data["journey_id"]
    topic      = data["topic"]
    videos     = data["video_topics"]
    batch_size = data.get("batch_size", 2)

    record = create_journey(journey_id, topic, videos, batch_size)

    batch_1 = get_batch_topics(journey_id, 1)
    dispatch("video_batch.yml", {
        "journey_id":    journey_id,
        "journey_topic": topic,
        "batch_num":     "1",
        "topics_json":   json.dumps(batch_1)
    })

    return jsonify({"status": "ok", "total_batches": record["total_batches"]})


# ─── Slack interactive actions ────────────────────────────────────────────────

@app.post("/slack/actions")
def handle_actions():
    if not verify_slack(request):
        return jsonify({"error": "Invalid signature"}), 401

    payload = json.loads(request.form.get("payload", "{}"))
    actions = payload.get("actions", [])
    if not actions:
        return jsonify({}), 200

    action    = actions[0]
    action_id = action.get("action_id", "")
    raw_value = action.get("value", "{}")

    try:
        value = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        value = {}

    if action_id == "approve_video":
        return _approve_video(value)

    if action_id == "revise_video":
        return _revise_video(value)

    if action_id == "rerun_video":
        return _rerun_video(value)

    if action_id == "start_batch":
        return _start_batch(value)

    # Unknown action — ack silently
    return jsonify({}), 200


def _approve_video(value: dict):
    topic      = value.get("topic", "")
    journey_id = value.get("journey_id", "")
    batch_num  = int(value.get("batch_num") or 0)

    if not journey_id or not batch_num:
        # Single-video workflow — nothing to track in state
        return jsonify({}), 200

    journey = update_video_status(journey_id, topic, batch_num, "approved")
    if not journey:
        return jsonify({}), 200

    if is_batch_complete(journey_id, batch_num):
        batch_topics   = get_batch_topics(journey_id, batch_num)
        completed      = [t["topic"] for t in batch_topics]
        total_batches  = journey["total_batches"]
        post_batch_complete(journey_id, journey["topic"], batch_num, total_batches, completed)

    return jsonify({}), 200


def _revise_video(value: dict):
    topic      = value.get("topic", "")
    journey_id = value.get("journey_id", "")
    batch_num  = int(value.get("batch_num") or 0)

    if journey_id and batch_num:
        update_video_status(journey_id, topic, batch_num, "revision_needed")

    post_revision_flagged(topic, journey_id)
    return jsonify({}), 200


def _rerun_video(value: dict):
    """
    Dispatches a fresh video_creation.yml run for the same topic.
    If the video belongs to a journey batch, resets its approval status
    to "pending" so the batch completion gate waits for the new version.
    """
    topic      = value.get("topic", "")
    journey_id = value.get("journey_id", "")
    batch_num  = int(value.get("batch_num") or 0)
    context    = value.get("context", "")

    if not topic:
        return jsonify({"text": ":x: Could not rerun — topic not found in button value."}), 200

    # Reset batch state so the old approval doesn't count
    if journey_id and batch_num:
        reset_video_status(journey_id, topic, batch_num)

    try:
        dispatch("video_creation.yml", {
            "topic":      topic,
            "context":    context,
            "journey_id": journey_id,
            "batch_num":  str(batch_num) if batch_num else ""
        })
    except Exception as e:
        slack_post(
            [{"type": "section", "text": {"type": "mrkdwn",
              "text": ":x: *Rerun failed to dispatch*\n*Topic:* " + topic + "\n*Error:* " + str(e)}}],
            "Rerun failed: " + topic
        )
        return jsonify({}), 200

    slack_post(
        [{"type": "section", "text": {"type": "mrkdwn",
          "text": ":repeat: *Rerun started* for *" + topic + "*. The previous result has been cleared. I'll post here when the new version is ready for review."}}],
        "Rerun started: " + topic
    )
    return jsonify({}), 200


def _start_batch(value: dict):
    journey_id = value.get("journey_id", "")
    batch_num  = int(value.get("batch_num", 1))

    journey = get_journey(journey_id)
    if not journey:
        return jsonify({"text": ":x: Journey not found."}), 200

    topics = get_batch_topics(journey_id, batch_num)
    if not topics:
        return jsonify({"text": ":x: No topics found for this batch."}), 200

    advance_batch(journey_id)

    dispatch("video_batch.yml", {
        "journey_id":    journey_id,
        "journey_topic": journey["topic"],
        "batch_num":     str(batch_num),
        "topics_json":   json.dumps(topics)
    })

    return jsonify({}), 200


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
