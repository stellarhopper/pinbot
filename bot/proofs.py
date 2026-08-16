"""Proof photo handling — the part that has to survive a multi-day event.

Discord's attachment CDN URLs are signed and expire after roughly 24 hours, so
**no URL is ever persisted**. Instead the bot re-uploads the submitted photo as
a real attachment on its own message and stores that message's channel/message
IDs plus its jump URL, which never expires. Two consequences:

* Historical announcements keep rendering, because the embed points at the
  photo with the relative ``attachment://<filename>`` scheme rather than an
  absolute CDN URL. The client resolves that reference every time the message
  is viewed, so a day-three scroll back to a day-one crown still shows the
  photo.
* ``/hs`` needs an absolute URL for its thumbnail, so it re-fetches the proof
  message on demand and uses the freshly-signed URL from that live API
  response. :class:`ProofURLCache` keeps a four-table listing from making four
  API calls every time.

Image bytes exist only in memory, never on disk: the deployment target is a
Raspberry Pi and nothing may accumulate there.
"""

from __future__ import annotations

import io
import logging
import time

import discord

log = logging.getLogger(__name__)

MAX_PROOF_BYTES = 10 * 1024 * 1024

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


class ProofError(Exception):
    """A user-facing problem with the submitted photo."""


def validate(attachment: discord.Attachment) -> None:
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        raise ProofError(
            "That attachment isn't an image. Attach a photo of the score screen."
        )
    if attachment.size > MAX_PROOF_BYTES:
        raise ProofError(
            f"That photo is {attachment.size / 1_048_576:.1f} MB. "
            f"Keep it under {MAX_PROOF_BYTES // 1_048_576} MB."
        )


def proof_filename(attachment: discord.Attachment) -> str:
    """A safe, predictable filename for the re-uploaded attachment.

    Deliberately not derived from user input beyond the extension — the name is
    referenced by the embed's ``attachment://`` URL, so it must not contain
    anything that needs escaping.
    """
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    ext = _EXT_BY_CONTENT_TYPE.get(content_type)
    if ext is None:
        original = (attachment.filename or "").lower()
        for candidate in _EXT_BY_CONTENT_TYPE.values():
            if original.endswith(candidate):
                ext = candidate
                break
    return f"proof{ext or '.png'}"


async def read_proof(attachment: discord.Attachment) -> tuple[bytes, str]:
    """Validate and read an attachment into memory. Returns (bytes, filename)."""
    validate(attachment)
    try:
        data = await attachment.read()
    except discord.HTTPException as exc:
        raise ProofError(
            "Discord wouldn't give me that photo. Try submitting again."
        ) from exc
    return data, proof_filename(attachment)


def as_file(data: bytes, filename: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=filename)


# What the vision API will accept. Deliberately narrower than what /new accepts:
# a phone can upload HEIC/HEIF straight from the camera roll, and that is a
# perfectly good proof photo that simply cannot be sent for a machine read.
_VISION_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def vision_media_type(attachment: discord.Attachment) -> str | None:
    """The media type to send to the vision API, or None if it can't be read.

    None means "skip the check", never "reject the photo" — the score is
    already on the ledger by the time anything asks this.
    """
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    return content_type if content_type in _VISION_MEDIA_TYPES else None


def _image_url(message: discord.Message) -> str | None:
    """Find the proof photo on one of the bot's own messages.

    Two shapes exist, and the difference is not obvious:

    * A compact non-crown post is a plain message with a file, so the photo is
      in ``message.attachments``.
    * A crown announcement embeds the photo with ``attachment://``. Discord
      moves that attachment **into the embed and removes it from**
      ``message.attachments``, leaving the list empty — and crown
      announcements are precisely the messages ``/hs`` displays.

    Checking only the attachment list therefore finds nothing for every current
    king, which silently costs the standings their thumbnails.
    """
    if message.attachments:
        return message.attachments[0].url
    for embed in message.embeds:
        if embed.image and embed.image.url:
            return embed.image.url
        if embed.thumbnail and embed.thumbnail.url:
            return embed.thumbnail.url
    return None


class ProofURLCache:
    """Short-lived cache of freshly-signed attachment URLs.

    A cached URL is valid for far longer than the TTL; the TTL exists to bound
    staleness, not to track expiry. On any failure the caller falls back to the
    permanent jump link rather than erroring.
    """

    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl = ttl_seconds
        self._cache: dict[int, tuple[float, str | None]] = {}

    async def fresh_url(
        self, client: discord.Client, channel_id: int | None, message_id: int | None
    ) -> str | None:
        if not channel_id or not message_id:
            return None
        cached = self._cache.get(message_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        url: str | None = None
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                message = await channel.fetch_message(message_id)
                url = _image_url(message)
        except discord.Forbidden:
            # The single most likely cause, and previously invisible: without
            # Read Message History the standings silently lose every photo and
            # show only the jump link.
            log.warning(
                "cannot read message history in channel %s, so /hs will show proof "
                "links instead of photos — grant the bot Read Message History there",
                channel_id,
            )
        except (discord.HTTPException, discord.InvalidData):
            # A deleted message, a revoked channel, or a rate limit. Falling
            # back to the permanent jump link is the intended behaviour.
            url = None

        self._cache[message_id] = (time.monotonic() + self.ttl, url)
        return url

    def forget(self, message_id: int) -> None:
        self._cache.pop(message_id, None)
