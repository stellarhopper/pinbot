"""The photo check's two invariants: it never rejects, and it never raises.

`bot.vision` imports `anthropic` *inside* check_score, so a fake module in
sys.modules is enough to drive every branch — no dependency, no network, no key.
"""

from __future__ import annotations

import base64
import importlib.machinery
import io
import json
import sys
import types

import pytest

from bot import vision

CLAIMED = 3_127_605_730
IMAGE = b"\xff\xd8\xff\xe0 not really a jpeg"


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, payload: dict | None, *, stop_reason: str = "end_turn") -> None:
        self.stop_reason = stop_reason
        self.content = [FakeBlock(json.dumps(payload))] if payload is not None else []


class FakeMessages:
    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeClient:
    last: "FakeClient | None" = None

    def __init__(self, outcome) -> None:
        self.messages = FakeMessages(outcome)
        FakeClient.last = self


@pytest.fixture()
def anthropic_double(monkeypatch):
    """Install a fake `anthropic` and a key, and hand back a setter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")

    def install(outcome):
        module = types.ModuleType("anthropic")
        # is_available() goes through importlib.util.find_spec, which raises on
        # a module in sys.modules whose __spec__ is None — as a bare
        # ModuleType's is. Give it one so the fake looks genuinely importable.
        module.__spec__ = importlib.machinery.ModuleSpec("anthropic", loader=None)
        module.AsyncAnthropic = lambda *a, **kw: FakeClient(outcome)
        monkeypatch.setitem(sys.modules, "anthropic", module)
        return module

    return install


def payload(score, *, legible=True, table_name="Godzilla", reasoning="saw it"):
    return {
        "score": score,
        "legible": legible,
        "table_name": table_name,
        "reasoning": reasoning,
    }


# ------------------------------------------------------------------- verdicts

async def test_a_matching_read_is_a_match(anthropic_double):
    anthropic_double(FakeResponse(payload(CLAIMED)))
    result = await vision.check_score(IMAGE, "image/jpeg", CLAIMED)
    assert result.verdict == "match"
    assert result.score == CLAIMED
    assert not result.needs_review


async def test_a_different_read_is_a_mismatch(anthropic_double):
    anthropic_double(FakeResponse(payload(999)))
    result = await vision.check_score(IMAGE, "image/jpeg", CLAIMED)
    assert result.verdict == "mismatch"
    assert result.score == 999
    assert result.needs_review


@pytest.mark.parametrize(
    "body",
    [payload(None, legible=False), payload(None), payload(CLAIMED, legible=False)],
    ids=["null-and-illegible", "null-score", "illegible-flag"],
)
async def test_an_unreadable_photo_is_flagged_not_guessed(anthropic_double, body):
    """Unreadable proof is not proof; it gets a human sign-off rather than a pass."""
    anthropic_double(FakeResponse(body))
    result = await vision.check_score(IMAGE, "image/jpeg", CLAIMED)
    assert result.verdict == "illegible"
    assert result.needs_review


# ------------------------------- never fails a submission (the hard invariant)

@pytest.mark.parametrize(
    "outcome",
    [
        RuntimeError("connection reset"),
        TimeoutError(),
        FakeResponse(payload(CLAIMED), stop_reason="refusal"),
        FakeResponse(None),
        FakeResponse({"not": "the schema"}),
        FakeResponse(["not even an object"]),
        FakeResponse("a bare string"),
    ],
    ids=[
        "network-error",
        "timeout",
        "refusal",
        "empty-response",
        "junk-payload",
        "json-list",
        "json-string",
    ],
)
async def test_every_failure_becomes_unavailable_and_never_raises(
    anthropic_double, outcome
):
    """A score is already on the ledger by the time this runs. Whatever goes
    wrong here, it must cost the player nothing."""
    anthropic_double(outcome)
    result = await vision.check_score(IMAGE, "image/jpeg", CLAIMED)
    assert result.verdict == "unavailable"
    assert not result.needs_review, "the bot's own failure is never the player's problem"


async def test_no_key_means_no_call_at_all(monkeypatch, anthropic_double):
    anthropic_double(FakeResponse(payload(CLAIMED)))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    FakeClient.last = None
    result = await vision.check_score(IMAGE, "image/jpeg", CLAIMED)
    assert result.verdict == "unavailable"
    assert FakeClient.last is None, "no key must mean no billable request"


def test_is_available_needs_both_the_key_and_the_package(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not vision.is_available()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    monkeypatch.setattr(vision.importlib.util, "find_spec", lambda name: None)
    assert not vision.is_available(), "a key alone isn't enough — the Pi needs the package"


# ----------------------------------------------------------- request shape

async def test_the_request_sends_the_image_and_asks_for_low_effort(anthropic_double):
    anthropic_double(FakeResponse(payload(CLAIMED)))
    await vision.check_score(IMAGE, "image/png", CLAIMED)

    sent = FakeClient.last.messages.calls[0]
    assert sent["model"] == vision.MODEL
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["schema"] == vision._SCHEMA

    image = sent["messages"][0]["content"][0]
    assert image["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(image["source"]["data"]) == IMAGE


# -------------------------------------------------------- oversized photos

def jpeg(width: int, height: int, *, orientation: int | None = None) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    Image.new("RGB", (width, height), "white").save(out, "JPEG", exif=exif)
    return out.getvalue()


def dimensions(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def test_a_photo_the_api_would_take_is_sent_byte_for_byte():
    """A re-encode is lossy, and a second JPEG pass smears segment displays."""
    photo = jpeg(4032, 3024)
    assert vision.fit_for_api(photo, "image/jpeg") == (photo, "image/jpeg")


def test_a_photo_too_heavy_for_the_api_is_shrunk(monkeypatch):
    # A genuinely 10 MB photo is slow to build; lower the ceiling instead.
    photo = jpeg(4032, 3024)
    monkeypatch.setattr(vision, "_MAX_ENCODED_BYTES", len(photo))
    data, media_type = vision.fit_for_api(photo, "image/png")
    assert media_type == "image/jpeg"
    assert max(dimensions(data)) == vision._TARGET_SIDE


def test_a_photo_too_wide_for_the_api_is_shrunk():
    """A 50 MP phone sensor is 8160 px wide; the API refuses anything over 8000."""
    data, _ = vision.fit_for_api(jpeg(8160, 6120), "image/jpeg")
    assert dimensions(data) == (2576, 1932)


def test_a_shrunk_photo_keeps_the_phone_s_rotation():
    """Phones say "rotate me" in EXIF, and the re-encode drops EXIF."""
    data, _ = vision.fit_for_api(jpeg(8160, 6120, orientation=6), "image/jpeg")
    assert dimensions(data) == (1932, 2576)


def test_bytes_pillow_cannot_read_go_through_for_the_api_to_judge():
    assert vision.fit_for_api(IMAGE, "image/jpeg") == (IMAGE, "image/jpeg")


async def test_the_check_sends_the_shrunk_photo(anthropic_double):
    anthropic_double(FakeResponse(payload(CLAIMED)))
    await vision.check_score(jpeg(8160, 6120), "image/jpeg", CLAIMED)

    image = FakeClient.last.messages.calls[0]["messages"][0]["content"][0]
    assert max(dimensions(base64.standard_b64decode(image["source"]["data"]))) == 2576


# ------------------------------------------------------------- wrong table

async def test_a_wrong_table_is_reported_but_does_not_flag_on_its_own(anthropic_double):
    """Backglass art is often out of frame, so this stays advisory until we've
    seen how reliable the reads are."""
    anthropic_double(FakeResponse(payload(CLAIMED, table_name="Attack From Mars")))
    result = await vision.check_score(IMAGE, "image/jpeg", CLAIMED)
    assert result.verdict == "match"
    assert not result.needs_review
    assert result.wrong_table("Godzilla")


@pytest.mark.parametrize(
    "read,claimed,expected",
    [
        ("Godzilla", "godzilla", False),
        ("  Godzilla ", "Godzilla", False),
        (None, "Godzilla", False),
        ("", "Godzilla", False),
        ("Attack From Mars", "Godzilla", True),
    ],
)
def test_wrong_table_ignores_case_spacing_and_unknowns(read, claimed, expected):
    assert vision.VisionResult("match", table_name=read).wrong_table(claimed) is expected
