"""
slack_notifier.py

Handles all outbound Slack messages for the content automation pipeline.

Actions (passed as first CLI argument):
  video_ready   — notify that a video is done and needs human review
  post_journey  — upload the journey spreadsheet and register state with relay

Usage:
  python agents/slack_notifier.py video_ready
  python agents/slack_notifier.py post_journey
"""

import os
import sys
import json
import requests


SLACK_API = "https://slack.com/api"


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}",
        "Content-Type": "application/json"
    }


def _post(endpoint: str, payload: dict) -> dict:
    resp = requests.post(f"{SLACK_API}/{endpoint}", headers=_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error on {endpoint}: {data.get('error')}")
    return data


# ─── Video ready notification ─────────────────────────────────────────────────

def notify_video_ready(topic: str, notebook_url: str, journey_id: str = "", batch_num: str = "", context: str = ""):
    """
    Post a Slack message when a NotebookLM video is ready for human review.
    Includes Approve, Flag for Revision, and Rerun buttons.
    Context is embedded in the button value so Rerun can replay the exact
    same topic + context without the user having to retype anything.
    """
    context_line = ""
    if journey_id:
        context_line = f"\n*Journey batch:* {batch_num}"

    # Value attached to every button so the relay knows what to act on.
    # context is included so Rerun can redispatch with the original inputs.
    btn_value = json.dumps({
        "topic":      topic,
        "context":    context,
        "journey_id": journey_id,
        "batch_num":  str(batch_num)
    })

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":clapper:  Video ready for review"}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Topic:* {topic}{context_line}\n*Status:* Generation complete — awaiting review"
            },
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open in NotebookLM"},
                "url": notebook_url,
                "action_id": "open_notebook"
            }
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text":  {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_video",
                    "value": btn_value
                },
                {
                    "type": "button",
                    "text":  {"type": "plain_text", "text": "Flag for revision"},
                    "style": "danger",
                    "action_id": "revise_video",
                    "value": btn_value
                },
                {
                    "type": "button",
                    "text":  {"type": "plain_text", "text": "Rerun video"},
                    "action_id": "rerun_video",
                    "value": btn_value
                }
            ]
        }
    ]

    _post("chat.postMessage", {
        "channel": os.environ["SLACK_CHANNEL_ID"],
        "blocks":  blocks,
        "text":    f"Video ready for review: {topic}"   # fallback for notifications
    })
    print(f"Slack: video ready notification sent for '{topic}'")


def notify_cinematic_unavailable(topic: str, notebook_url: str = "", journey_id: str = "", batch_num: str = ""):
    """
    Post the specific notification when Cinematic video style is not
    selectable in NotebookLM. Uses the exact message text requested so
    the team knows immediately what the limitation is.
    """
    context_line = ("\n*Journey batch:* " + batch_num) if journey_id else ""

    body = (
        ":movie_camera: *Cinematic videos are not available at this time.*\n"
        "*Topic:* " + topic + context_line + "\n"
        "The Cinematic style option could not be selected in NotebookLM. "
        "This is typically a NotebookLM usage limit or temporary restriction. "
        "Try again later or generate the video manually."
    )

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": body
            }
        }
    ]

    if notebook_url:
        blocks[0]["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Open notebook"},
            "url": notebook_url,
            "action_id": "open_cinematic_unavailable_notebook"
        }

    _post("chat.postMessage", {
        "channel": os.environ["SLACK_CHANNEL_ID"],
        "blocks":  blocks,
        "text":    "Cinematic videos are not available at this time."
    })
    print(f"Slack: cinematic unavailable notification sent for '{topic}'")


def notify_video_error(topic: str, error: str, notebook_url: str = ""):
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":warning: *Video generation failed*\n"
                    f"*Topic:* {topic}\n"
                    f"*Error:* {error}"
                )
            }
        }
    ]
    if notebook_url:
        blocks[0]["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Open notebook"},
            "url":  notebook_url,
            "action_id": "open_failed_notebook"
        }

    _post("chat.postMessage", {
        "channel": os.environ["SLACK_CHANNEL_ID"],
        "blocks":  blocks,
        "text":    f"Video generation failed: {topic}"
    })
    print(f"Slack: error notification sent for '{topic}'")


# ─── Journey spreadsheet upload ───────────────────────────────────────────────

def post_spreadsheet(filepath: str, journey_topic: str, total_videos: int):
    """Upload the journey xlsx to Slack and post an intro message."""
    filename = os.path.basename(filepath)

    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{SLACK_API}/files.upload",
            headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
            data={
                "channels": os.environ["SLACK_CHANNEL_ID"],
                "filename": filename,
                "title":    f"{journey_topic} — Journey Template",
                "initial_comment": (
                    f":clipboard: *Journey template generated for {journey_topic}*\n"
                    f"The spreadsheet contains {total_videos} video activities. "
                    f"Review the template before video generation begins. "
                    f"Video production will start automatically — "
                    f"you will receive a notification when the first two videos are ready."
                )
            },
            files={"file": f},
            timeout=30
        )

    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack file upload failed: {data.get('error')}")

    print(f"Slack: spreadsheet uploaded for '{journey_topic}'")
    return data


# ─── Register journey with relay and start batch 1 ───────────────────────────

def register_and_start(journey_data: dict, run_id: str):
    """
    POST journey state to the webhook relay so it can manage batching.
    The relay immediately dispatches batch 1 on receipt.
    """
    relay_url    = os.environ.get("RELAY_URL", "").rstrip("/")
    relay_secret = os.environ.get("RELAY_SECRET", "")

    if not relay_url:
        print("WARNING: RELAY_URL not set — skipping relay registration.")
        return

    payload = {
        "journey_id":   run_id,
        "topic":        journey_data["journey_topic"],
        "video_topics": journey_data["video_topics"],
        "batch_size":   2
    }

    resp = requests.post(
        f"{relay_url}/journey/register",
        headers={
            "Content-Type":    "application/json",
            "X-Relay-Secret":  relay_secret
        },
        json=payload,
        timeout=15
    )

    if resp.ok:
        info = resp.json()
        print(
            f"Relay: journey registered. "
            f"Total batches: {info.get('total_batches')} — batch 1 dispatched."
        )
    else:
        print(f"WARNING: Relay registration failed ({resp.status_code}): {resp.text}")


# ─── CLI dispatch ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""

    if action == "video_ready":
        with open("/tmp/notebooklm_result.json") as f:
            result = json.load(f)

        # Pull context from video_meta.json so the Rerun button can reuse it
        context = ""
        try:
            with open("/tmp/video_meta.json") as f:
                meta = json.load(f)
            context = meta.get("context", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        topic        = result.get("topic", "Unknown topic")
        notebook_url = result.get("notebook_url", "")
        journey_id   = result.get("journey_id") or os.environ.get("JOURNEY_ID", "")
        batch_num    = result.get("batch_num") or os.environ.get("BATCH_NUM", "")

        if result.get("success"):
            notify_video_ready(topic, notebook_url, journey_id, batch_num, context)
        elif result.get("error_code") == "cinematic_unavailable":
            notify_cinematic_unavailable(topic, notebook_url, journey_id, batch_num)
        else:
            notify_video_error(topic, result.get("error", "Unknown error"), notebook_url)

    elif action == "post_journey":
        with open("/tmp/journey_videos.json") as f:
            journey_data = json.load(f)

        run_id = os.environ.get("GH_RUN_ID", "unknown")

        post_spreadsheet(
            filepath=journey_data["spreadsheet_path"],
            journey_topic=journey_data["journey_topic"],
            total_videos=len(journey_data["video_topics"])
        )

        register_and_start(journey_data, run_id)

    else:
        print(f"Unknown action: '{action}'. Use 'video_ready' or 'post_journey'.")
        sys.exit(1)
