"""Phase 2, opt-in: cross-check the claimed score against the photo.

Deliberately not wired into the submission path yet. Two properties this module
must keep whenever it is:

* **It never rejects a submission.** A mismatch flags the score for admin
  review; the score stands until a human acts. Segment and DMD displays are
  genuinely hard to read, so a false positive must never block a legitimate
  player mid-tournament.
* **It never fails a submission.** A missing key, missing package, timeout, or
  API error records "unavailable" and returns; the score is already accepted by
  the time this runs.

Both the ``anthropic`` package and ``ANTHROPIC_API_KEY`` are optional. Absent
either, :func:`is_available` is False and ``/config vision on`` refuses.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": ["integer", "null"],
            "description": "The score read from the display, or null if unreadable.",
        },
        "legible": {
            "type": "boolean",
            "description": "Whether a score was clearly readable in the photo.",
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence on what was visible.",
        },
    },
    "required": ["score", "legible", "reasoning"],
    "additionalProperties": False,
}

_PROMPT = (
    "This is a photo of a pinball machine's score display, taken by a player "
    "reporting their score in a tournament. Read the score shown for the "
    "player being reported. Pinball displays are often dot-matrix, segmented, "
    "or reflective, and photos are taken at an angle in poor light — if you "
    "cannot read a score confidently, set legible to false and score to null "
    "rather than guessing."
)


@dataclass(frozen=True, slots=True)
class VisionResult:
    verdict: str  # "match" | "mismatch" | "illegible" | "unavailable"
    score: int | None = None
    reasoning: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.verdict == "mismatch"


def is_available() -> bool:
    """True when both the key and the package are present on this host."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    return importlib.util.find_spec("anthropic") is not None


async def check_score(
    image_bytes: bytes, media_type: str, claimed_score: int
) -> VisionResult:
    """Read the score off the photo and compare it to what was claimed.

    Never raises: every failure path returns a VisionResult.
    """
    if not is_available():
        return VisionResult("unavailable", reasoning="not configured on this host")
    try:
        import anthropic

        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(image_bytes).decode(),
                            },
                        },
                        {"type": "text", "text": _PROMPT},
                    ],
                }
            ],
        )
        if response.stop_reason == "refusal":
            return VisionResult("unavailable", reasoning="request was declined")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return VisionResult("unavailable", reasoning="empty response")
        payload = json.loads(text)
    except Exception:  # noqa: BLE001 - a check must never break a submission
        log.exception("vision check failed")
        return VisionResult("unavailable", reasoning="check errored")

    if not payload.get("legible") or payload.get("score") is None:
        return VisionResult("illegible", reasoning=payload.get("reasoning"))
    read = int(payload["score"])
    verdict = "match" if read == claimed_score else "mismatch"
    return VisionResult(verdict, score=read, reasoning=payload.get("reasoning"))
