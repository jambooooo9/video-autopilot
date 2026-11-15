"""Stage 1 — pick a fresh, viral-leaning idea for the channel's niche."""

from __future__ import annotations

from .config import load_prompt
from .knowledge import strategy_notes
from .llm import complete
from .utils import extract_json, log, recent_topics


# JSON schema specifically for the idea-generation stage.
IDEA_SCHEMA = {
    "type": "object",
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "concept": {"type": "string"},
                    "primary_keyword": {"type": "string"},
                    "search_question": {"type": "string"},
                    "hook_angle": {"type": "string"},
                    "why_viral": {"type": "string"},
                    "save_worthiness": {"type": "integer"},
                },
                "required": [
                    "title",
                    "concept",
                    "primary_keyword",
                    "search_question",
                    "hook_angle",
                    "why_viral",
                    "save_worthiness",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ideas"],
    "additionalProperties": False,
}


def get_trends(cfg: dict) -> list[str]:
    """Optional trend seeds. Default: none."""

    return cfg["channel"].get("trend_seeds", [])


def generate_idea(cfg: dict) -> dict:
    ch = cfg["channel"]

    trends = get_trends(cfg)
    notes = strategy_notes(ch["key"])

    prompt = load_prompt("ideation").format(
        niche=ch["niche"],
        name=ch["name"],
        audience=ch["audience"],
        angle=ch["angle"],
        trends="\n".join(
            f"- {t}" for t in trends
        ) or "(none — use your niche expertise)",
        recent_topics="\n".join(
            f"- {t}" for t in recent_topics(ch["key"])
        ) or "(none yet)",
        creator_notes=notes or "(none provided)",
        n=6,
    )

    data = extract_json(
        complete(
            prompt,
            cfg,
            max_tokens=1200,
            response_schema=IDEA_SCHEMA,
        )
    )

    ideas = data.get("ideas", [])

    if not ideas:
        raise RuntimeError("Ideation returned no ideas.")

    best = ideas[0]

    log(
        f'idea: "{best["title"]}" '
        f'(save-worthiness {best.get("save_worthiness", "?")}/5)',
        "ok",
    )

    return best
