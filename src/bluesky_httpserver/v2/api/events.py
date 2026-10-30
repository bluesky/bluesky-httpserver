"""Routes of the event log."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from .dependencies import ReadPrincipal, Services, ServicesDep
from .errors import ApiError, error_responses
from .models import EventPage, EventView

router = APIRouter(tags=["events"])

_EVENT_ID = re.compile(r"0|[1-9][0-9]*")
_STREAM_BATCH = 100


@router.get("/events", response_model=EventPage, responses=error_responses("invalid_request"))
async def get_events(
    services: ServicesDep,
    principal: ReadPrincipal,
    after: Annotated[int, Query(ge=0, description="Return events whose `event_id` is greater than this.")] = 0,
    limit: Annotated[int, Query(ge=1, le=1000, description="Most events to return.")] = 100,
) -> EventPage:
    """Events after `after`, oldest first.

    To follow the log, pass the last `event_id` received as the next `after`, or use the event stream.
    """
    events = await services.store.events_after(after, limit)
    return EventPage(events=[EventView.from_record(event) for event in events])


@router.get(
    "/events/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "An event stream that ends when the caller's token expires.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **error_responses("invalid_request", "invalid_last_event_id"),
    },
)
async def stream_events(
    services: ServicesDep,
    principal: ReadPrincipal,
    after: Annotated[int, Query(ge=0, description="Send events whose `event_id` is greater than this.")] = 0,
    last_event_id: Annotated[
        str | None,
        Header(
            alias="Last-Event-ID",
            description="`event_id` of the last event received; takes precedence over `after`.",
        ),
    ] = None,
) -> StreamingResponse:
    """Server-sent events: every event after the cursor, then each new one as it is recorded.

    Each event is sent as `id: <event_id>`, `event: <event_type>` and `data: <event as JSON>` lines, the same
    event object as `GET /api/v2/events` returns. A `: heartbeat` comment is sent during silences.

    The cursor is `Last-Event-ID` if present, else `after`, so a reconnecting `EventSource` resumes where it
    left off. The stream ends when the caller's token expires; reconnect with a fresh token and
    `Last-Event-ID` to continue without gaps.
    """
    cursor = after
    if last_event_id is not None:
        if not _EVENT_ID.fullmatch(last_event_id):
            raise ApiError("invalid_last_event_id", "Last-Event-ID must be a non-negative integer")
        cursor = int(last_event_id)
    return StreamingResponse(
        _stream(services, cursor, principal.expires_at),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _stream(services: Services, cursor: int, expires_at: float) -> AsyncIterator[bytes]:
    while True:
        events = await services.store.events_after(cursor, limit=_STREAM_BATCH)
        for event in events:
            cursor = event.event_id
            data = EventView.from_record(event).model_dump_json()
            yield f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {data}\n\n".encode()
        if len(events) == _STREAM_BATCH:
            continue  # drain the backlog before waiting
        remaining = expires_at - services.clock()
        if remaining <= 0:
            return  # the token has expired; the client reconnects with a fresh one and Last-Event-ID
        # The heartbeat also bounds how long a silently disconnected client holds the stream.
        try:
            async with asyncio.timeout(min(services.sse_heartbeat_seconds, remaining)):
                await services.store.wait_for_event(cursor)
        except TimeoutError:
            yield b": heartbeat\n\n"
