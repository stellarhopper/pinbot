"""Where pinball commands may be run.

A tournament has one home: the channel holding the proof photos, the crown
announcements and the final results. Once that channel is set, answering
commands anywhere else splits the event in two — scores land in one place while
players are looking at another — and a `/new` run in the wrong channel silently
posts someone's photo somewhere they didn't expect.

So once a channel is configured, commands are answered there and politely
redirected everywhere else. Threads of that channel count as the channel.

`/config pinball-channel` is the deliberate exception. It is the command that
*moves* the home, so it has to work from wherever you happen to be standing —
otherwise a channel that got deleted, archived, or locked down would take the
whole bot with it, with no command left that could point it somewhere new.

The decision is a pure function so it can be tested without an interaction.
"""

from __future__ import annotations

# Qualified command names that work from any channel.
EXEMPT = frozenset({"config pinball-channel"})


def is_exempt(qualified_name: str | None) -> bool:
    return qualified_name in EXEMPT


def is_allowed(
    *, configured_id: int | None, channel_id: int | None, parent_id: int | None = None
) -> bool:
    """May a command run here?

    `parent_id` is the thread's parent, so a discussion thread inside the
    pinball channel is still the pinball channel.
    """
    if configured_id is None:
        # Nothing configured yet — this is the window in which setup runs and
        # the channel gets adopted, so everything is allowed everywhere.
        return True
    return channel_id == configured_id or parent_id == configured_id


def elsewhere_notice(configured_id: int) -> str:
    return (
        f"Pinball lives in <#{configured_id}> \N{EM DASH} run that there and I'll "
        "answer.\n_(Admins: `/config pinball-channel` works from anywhere, and "
        "with no argument it moves the tournament to the channel you're in.)_"
    )
