"""
state_store.py

Thread-safe JSON file store for journey batch state.
Persists between requests on the relay server (Render / Railway keeps the
filesystem alive for the lifetime of the service).

State schema per journey:
{
  "journey_id": {
    "topic":         str,
    "created_at":    ISO timestamp,
    "video_topics":  [{step_name, topic, copy, day}, ...],
    "batches":       [[{...}, {...}], [{...}], ...],   # grouped by batch_size
    "total_batches": int,
    "current_batch": int,                              # 1-indexed
    "batch_status":  {"1": {"Topic A": "approved", "Topic B": "pending"}, ...},
    "completed_videos": [topic_str, ...]
  }
}
"""

import json
import os
import threading
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ─── Public API ───────────────────────────────────────────────────────────────

def create_journey(journey_id: str, topic: str, video_topics: list, batch_size: int = 2) -> dict:
    """
    Register a new journey and split video_topics into batches of batch_size.
    Returns the created journey record.
    """
    with _lock:
        data = _load()
        batches = [
            video_topics[i:i + batch_size]
            for i in range(0, len(video_topics), batch_size)
        ]
        record = {
            "topic":           topic,
            "created_at":      datetime.now(timezone.utc).isoformat(),
            "video_topics":    video_topics,
            "batches":         batches,
            "total_batches":   len(batches),
            "current_batch":   1,
            "batch_status":    {},
            "completed_videos": []
        }
        data[journey_id] = record
        _save(data)
        return record


def get_journey(journey_id: str) -> dict | None:
    with _lock:
        return _load().get(journey_id)


def get_batch_topics(journey_id: str, batch_num: int) -> list:
    """Return the list of video topic dicts for a given 1-indexed batch number."""
    with _lock:
        data = _load()
        journey = data.get(journey_id, {})
        batches = journey.get("batches", [])
        if batch_num < 1 or batch_num > len(batches):
            return []
        return batches[batch_num - 1]


def update_video_status(journey_id: str, topic: str, batch_num: int, status: str) -> dict | None:
    """
    Set the status of a single video within a batch.
    status: "approved" | "revision_needed" | "pending"
    Returns the updated journey record, or None if journey not found.
    """
    with _lock:
        data = _load()
        journey = data.get(journey_id)
        if not journey:
            return None

        bkey = str(batch_num)
        if bkey not in journey["batch_status"]:
            journey["batch_status"][bkey] = {}

        journey["batch_status"][bkey][topic] = status

        if status == "approved" and topic not in journey["completed_videos"]:
            journey["completed_videos"].append(topic)

        _save(data)
        return journey


def reset_video_status(journey_id: str, topic: str, batch_num: int) -> dict | None:
    """
    Reset a single video back to "pending" when a rerun is triggered.
    Called before dispatching the new workflow so the batch completion
    check won't consider the old approval still valid.
    Returns the updated journey record, or None if journey not found.
    """
    with _lock:
        data = _load()
        journey = data.get(journey_id)
        if not journey:
            return None

        bkey = str(batch_num)
        if bkey not in journey["batch_status"]:
            journey["batch_status"][bkey] = {}

        journey["batch_status"][bkey][topic] = "pending"

        # Remove from completed_videos so the journey-complete check
        # doesn't count this video until it's re-approved
        if topic in journey["completed_videos"]:
            journey["completed_videos"].remove(topic)

        _save(data)
        return journey


def is_batch_complete(journey_id: str, batch_num: int) -> bool:
    """
    Returns True when every video in the batch is marked "approved".
    Videos marked "revision_needed" block completion.
    """
    with _lock:
        data = _load()
        journey = data.get(journey_id, {})
        batches = journey.get("batches", [])

        if batch_num < 1 or batch_num > len(batches):
            return False

        batch_topics = [t["topic"] for t in batches[batch_num - 1]]
        statuses = journey.get("batch_status", {}).get(str(batch_num), {})

        return all(statuses.get(t) == "approved" for t in batch_topics)


def advance_batch(journey_id: str) -> int:
    """
    Increment current_batch and return the new value.
    Returns -1 if the journey is already on its last batch.
    """
    with _lock:
        data = _load()
        journey = data.get(journey_id)
        if not journey:
            return -1

        current = journey["current_batch"]
        if current >= journey["total_batches"]:
            return -1   # already on last batch

        next_batch = current + 1
        journey["current_batch"] = next_batch
        _save(data)
        return next_batch


def is_journey_complete(journey_id: str) -> bool:
    """Returns True when all videos across all batches are approved."""
    with _lock:
        data = _load()
        journey = data.get(journey_id, {})
        all_topics = [t["topic"] for t in journey.get("video_topics", [])]
        completed = journey.get("completed_videos", [])
        return bool(all_topics) and all(t in completed for t in all_topics)
