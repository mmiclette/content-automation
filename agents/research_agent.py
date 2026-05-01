"""
research_agent.py

Calls the Claude API with the video research prompt to generate:
  1. A NotebookLM source document (1,200-1,800 word narrative)
  2. A NotebookLM Steering Prompt (4-part customization input)

When sequence metadata is passed (journey videos), the prompt gains a
JOURNEY SEQUENCE CONTEXT block that tells the agent what the previous
video already covered, what the next video will cover, and how this
video should position itself within the program arc.

Outputs are written to /tmp/ for the notebooklm_agent.py step.

Usage:
  # Standalone video
  python agents/research_agent.py "Depression and Daily Functioning" "Focus on working adults"

  # Journey video (all args required when sequence_position is passed)
  python agents/research_agent.py "Topic" "copy context" "2" "6" '{"previous":{...},"next":{...}}' "Journey Name"
"""

import os
import sys
import json
import anthropic


PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "video_research.txt")


def load_base_prompt():
    with open(PROMPT_PATH) as f:
        return f.read()


def build_sequence_block(
    sequence_position,
    total_videos,
    adjacent_context,
    journey_name
):
    """
    Build the JOURNEY SEQUENCE CONTEXT block injected into the prompt
    when this video is part of a multi-video program.

    The block tells the research agent three things:
      1. Where this video sits in the sequence
      2. What the previous video already established (do not repeat)
      3. What the next video will cover (do not front-run)

    This drives narrative continuity across the full program without
    requiring the agent to see all source documents simultaneously.
    """
    prev = adjacent_context.get("previous") if adjacent_context else None
    nxt  = adjacent_context.get("next")     if adjacent_context else None

    if prev:
        prev_line = (
            f"The previous video covered: \"{prev['topic']}\" "
            f"— {prev['key_points']}"
        )
    else:
        prev_line = (
            "This is the first video in the program. "
            "Establish the foundational concepts the remaining videos will build on."
        )

    if nxt:
        next_line = (
            f"The next video will cover: \"{nxt['topic']}\" "
            f"— {nxt['key_points']}"
        )
    else:
        next_line = (
            "This is the final video in the program. "
            "Synthesize the program's arc and leave the audience with clear next steps."
        )

    return f"""
JOURNEY SEQUENCE CONTEXT

This video is number {sequence_position} of {total_videos} in the "{journey_name}" program.

{prev_line}

{next_line}

SEQUENCING REQUIREMENTS — follow these precisely:

1. DO NOT re-explain concepts the previous video already covered.
   You may reference them briefly by name (e.g., "building on the connection
   between mood and daily functioning we explored earlier") but do not
   reintroduce or re-define them.

2. Open with an explicit narrative bridge. The first paragraph must connect
   to what the previous video established and state how this video advances
   that argument. Do not open as if this were a standalone piece.

3. DO NOT cover topics reserved for the next video. You may name them as
   "what comes next" in the closing section, but do not explain them.

4. The central argument for this video must advance the program's overall
   arc — it should only make full sense to someone who has watched the
   previous videos. A viewer dropping in here should feel they are joining
   a program in progress, not starting fresh.

5. The closing section should hand off cleanly to the next video's topic
   by naming it and briefly framing why it follows logically from this one.
"""


def generate_video_content(
    topic,
    context="",
    sequence_position=None,
    total_videos=None,
    adjacent_context=None,
    journey_name=""
):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    base_prompt = load_base_prompt()

    # Build the sequence context block if this is a journey video
    is_journey_video = (
        sequence_position is not None
        and total_videos is not None
        and total_videos > 1
    )

    sequence_block = ""
    if is_journey_video:
        sequence_block = build_sequence_block(
            sequence_position,
            total_videos,
            adjacent_context or {},
            journey_name or "this program"
        )

    context_block = f"\nACTIVITY CONTEXT: {context}" if context.strip() else ""

    user_message = f"""{base_prompt}
{sequence_block}
---

TOPIC: {topic}{context_block}

Generate the SOURCE DOCUMENT and STEERING PROMPT now."""

    label = f"[{sequence_position}/{total_videos}] " if is_journey_video else ""
    print(f"Research agent: {label}{topic}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": user_message}]
    )

    full_text = response.content[0].text.strip()

    # Parse labeled sections
    source_doc     = ""
    steering_prompt = ""

    if "SOURCE DOCUMENT:" in full_text and "STEERING PROMPT:" in full_text:
        parts      = full_text.split("STEERING PROMPT:", 1)
        source_part = parts[0].split("SOURCE DOCUMENT:", 1)
        source_doc      = source_part[1].strip() if len(source_part) > 1 else ""
        steering_prompt = parts[1].strip()
    else:
        source_doc = full_text
        steering_prompt = (
            f"This video argues that understanding {topic} gives people practical tools "
            f"to improve their health outcomes. "
            f"The audience is general adult patients. The tone is warm, calm, and encouraging. "
            f"The video moves from what this topic means, to why it matters, to what people "
            f"can do today. "
            f"Visual style: Clean, modern, warm illustration style. Soft natural lighting. "
            f"Diverse individuals in everyday settings including homes, parks, and welcoming "
            f"clinical spaces. Calm color palette with muted greens, soft blues, and warm "
            f"neutrals. No graphic or distressing imagery. Faces show calm, thoughtful, or "
            f"gently hopeful expressions. Show only calm, safe, and hopeful imagery."
        )

    word_count = len(source_doc.split())
    print(f"  Source document: {word_count} words")

    if word_count < 1200:
        print("  WARNING: Source document below 1,200-word minimum. Video quality may be limited.")

    return {
        "topic":           topic,
        "context":         context,
        "source_document": source_doc,
        "steering_prompt": steering_prompt,
        "word_count":      word_count,
        "is_journey_video": is_journey_video,
        "sequence_position": sequence_position,
        "total_videos":    total_videos
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agents/research_agent.py <topic> [context] "
              "[sequence_position] [total_videos] [adjacent_context_json] [journey_name]")
        sys.exit(1)

    topic              = sys.argv[1]
    context            = sys.argv[2] if len(sys.argv) > 2 else ""
    sequence_position  = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
    total_videos       = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
    journey_name       = sys.argv[6] if len(sys.argv) > 6 else ""

    adjacent_context = None
    if len(sys.argv) > 5 and sys.argv[5]:
        try:
            adjacent_context = json.loads(sys.argv[5])
        except json.JSONDecodeError:
            print("WARNING: Could not parse adjacent_context JSON. Proceeding without it.")

    result = generate_video_content(
        topic,
        context,
        sequence_position,
        total_videos,
        adjacent_context,
        journey_name
    )

    with open("/tmp/source_document.txt", "w") as f:
        f.write(result["source_document"])

    with open("/tmp/steering_prompt.txt", "w") as f:
        f.write(result["steering_prompt"])

    with open("/tmp/video_meta.json", "w") as f:
        json.dump({
            "topic":             result["topic"],
            "context":           result["context"],
            "word_count":        result["word_count"],
            "is_journey_video":  result["is_journey_video"],
            "sequence_position": result["sequence_position"],
            "total_videos":      result["total_videos"]
        }, f, indent=2)

    print("Research agent complete.")
