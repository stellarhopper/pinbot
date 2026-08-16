"""Opt-in: cross-check the claimed score against the photo.

Called from the submission path once the score is already on the ledger, and
what it catches is *honest mistakes* — a fat-fingered digit, a photo of the
wrong machine, a score typed while the ball was still in play — while the
player is still standing at the machine. It is not the integrity backstop; the
tournament is reconciled against the machines' own high-score tables at the
end. Two properties this module must keep:

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
        "table_name": {
            "type": ["string", "null"],
            "description": (
                "The pinball machine in the photo, from the backglass or "
                "cabinet art, or null if it can't be identified."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence on what was visible.",
        },
    },
    "required": ["score", "legible", "table_name", "reasoning"],
    "additionalProperties": False,
}

_PROMPT = (
    "This is a photo of a pinball machine's score display, taken by a player "
    "reporting their score in a tournament. Read the score shown for the "
    "player being reported. Pinball displays are often dot-matrix, segmented, "
    "or reflective, and photos are taken at an angle in poor light — if you "
    "cannot read a score confidently, set legible to false and score to null "
    "rather than guessing. Also name the machine if the backglass or cabinet "
    "art identifies it, and null if it doesn't."
)


@dataclass(frozen=True, slots=True)
class VisionResult:
    verdict: str  # "match" | "mismatch" | "illegible" | "unavailable"
    score: int | None = None
    reasoning: str | None = None
    table_name: str | None = None

    @property
    def needs_review(self) -> bool:
        """Whether a human should look at this one.

        "illegible" counts: proof nobody can read is not proof, and the point
        of flagging it is to get an explicit human sign-off rather than to let
        it pass silently. "unavailable" never counts — that is the bot's own
        failure, and it must not cost a player anything.
        """
        return self.verdict in {"mismatch", "illegible"}

    def wrong_table(self, claimed: str) -> bool:
        """Whether the photo looks like a different machine than the one claimed.

        Reported in the flag text but deliberately never a flag on its own,
        until we've seen how reliable the reads are. Backglass art is often out
        of frame entirely, and a null here means "couldn't tell", not "wrong".
        """
        if not self.table_name:
            return False
        return self.table_name.strip().casefold() != claimed.strip().casefold()


def _why(exc: Exception) -> str:
    """A short, safe reason for an admin to read. Never includes the key."""
    status = getattr(exc, "status_code", None)
    text = str(exc)
    if "credit balance" in text or "Plans & Billing" in text:
        return "the Anthropic account is out of credit"
    if status == 401 or "authentication" in text.lower():
        return "the API key was rejected"
    if status == 429:
        return "rate limited"
    if status == 400:
        return "the API rejected the request"
    if isinstance(exc, TimeoutError):
        return "timed out"
    return f"{type(exc).__name__}"


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
            # Reading a display is perception, not reasoning, and this runs on
            # every submission — so effort is the cost lever that matters here.
            # Raise it if the reads start disappointing.
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
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
    except Exception as exc:  # noqa: BLE001 - a check must never break a submission
        reason = _why(exc)
        # Billing and auth failures stay broken until a human acts, so they are
        # worth naming rather than logging as one more transient error. The
        # first live run of this check failed every time on an empty credit
        # balance and looked exactly like a working check.
        log.error("vision check failed: %s", reason, exc_info=True)
        return VisionResult("unavailable", reasoning=reason)

    # A response that isn't the shape we asked for is our problem, not the
    # player's. Degrading it to "illegible" would be the worst failure mode
    # this bot has: schema drift on the API side would publicly flag and
    # @mention on every single submission. "unavailable" stays quiet.
    if not isinstance(payload, dict) or not {"score", "legible"} <= payload.keys():
        log.warning("vision response was not the shape we asked for: %r", payload)
        return VisionResult("unavailable", reasoning="unexpected response shape")

    table_name = payload.get("table_name")
    if not payload.get("legible") or payload.get("score") is None:
        return VisionResult(
            "illegible", reasoning=payload.get("reasoning"), table_name=table_name
        )
    read = int(payload["score"])
    verdict = "match" if read == claimed_score else "mismatch"
    return VisionResult(
        verdict,
        score=read,
        reasoning=payload.get("reasoning"),
        table_name=table_name,
    )
