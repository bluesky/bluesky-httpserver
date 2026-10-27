import pytest

from bluesky_httpserver.v2.store import (
    LEASE_TTL_MAX_SECONDS,
    LEASE_TTL_MIN_SECONDS,
    EventType,
    LeaseHeld,
    LeaseRequired,
)

from .conftest import acquire, cancel, reorder, replace, revision_and_event_count, submit

SECOND = 1_000_000


async def renew(store, ttl_seconds, principal="alice"):
    return await store.write(lambda tx: tx.renew_lease(principal=principal, ttl_seconds=ttl_seconds))


async def release(store, principal):
    await store.write(lambda tx: tx.release_lease(principal=principal))


async def override(store, principal, reason, ttl_seconds=60):
    return await store.write(
        lambda tx: tx.override_lease(principal=principal, reason=reason, ttl_seconds=ttl_seconds)
    )


@pytest.mark.asyncio
async def test_lease_lifecycle(store, clock):
    lease = await store.read(lambda tx: tx.lease())
    assert lease.holder == "alice"
    assert lease.expires_at == clock.now + 3600 * SECOND

    clock.advance(10)
    renewed = await renew(store, 60)
    assert renewed.lease_uid == lease.lease_uid
    assert renewed.expires_at == clock.now + 60 * SECOND
    assert await store.read(lambda tx: tx.lease()) == renewed

    with pytest.raises(LeaseHeld) as exc_info:
        await acquire(store, "bob")
    assert exc_info.value.holder == "alice"
    with pytest.raises(LeaseHeld):
        await acquire(store, "alice")
    with pytest.raises(LeaseRequired):
        await release(store, "bob")

    await release(store, "alice")
    assert await store.read(lambda tx: tx.lease()) is None
    await acquire(store, "bob")
    assert (await store.read(lambda tx: tx.lease())).holder == "bob"

    events = await store.events_after(0)
    assert [event.event_type for event in events] == [
        EventType.LEASE_ACQUIRED,
        EventType.LEASE_RENEWED,
        EventType.LEASE_RELEASED,
        EventType.LEASE_ACQUIRED,
    ]
    assert {event.queue_revision for event in events} == {0}
    assert (await store.read(lambda tx: tx.queue())).revision == 0


@pytest.mark.asyncio
async def test_expired_lease_is_absent(store, clock):
    clock.advance(3600)
    assert await store.read(lambda tx: tx.lease()) is None
    with pytest.raises(LeaseRequired):
        await submit(store, 0)
    with pytest.raises(LeaseRequired):
        await renew(store, 60)
    assert (await acquire(store, "bob")).holder == "bob"


@pytest.mark.asyncio
async def test_edits_need_the_callers_lease(store):
    a, revision = await submit(store, 0)
    uid = a.operation_uid
    before = await revision_and_event_count(store)
    for edit in (
        lambda: submit(store, revision, principal="bob"),
        lambda: replace(store, revision, uid, principal="bob"),
        lambda: cancel(store, revision, uid, principal="bob"),
        lambda: reorder(store, revision, [uid], principal="bob"),
        # The lease is checked before the revision.
        lambda: cancel(store, 99, uid, principal="bob"),
    ):
        with pytest.raises(LeaseRequired):
            await edit()
        assert await revision_and_event_count(store) == before


@pytest.mark.asyncio
async def test_override_displaces_holder(store):
    lease = await override(store, "bob", "alice left")
    assert lease.holder == "bob"
    assert await store.read(lambda tx: tx.lease()) == lease
    event = (await store.events_after(0))[-1]
    assert event.event_type is EventType.LEASE_OVERRIDDEN
    assert event.payload["previous_holder"] == "alice"
    assert event.payload["reason"] == "alice left"

    before = await revision_and_event_count(store)
    with pytest.raises(ValueError, match="blank"):
        await override(store, "carol", "  ")
    assert await revision_and_event_count(store) == before


@pytest.mark.asyncio
async def test_ttl_bounds(store, clock):
    for ttl_seconds in (LEASE_TTL_MIN_SECONDS - 1, LEASE_TTL_MAX_SECONDS + 1):
        with pytest.raises(ValueError, match="lease TTL"):
            await renew(store, ttl_seconds)
    for ttl_seconds in (LEASE_TTL_MIN_SECONDS, LEASE_TTL_MAX_SECONDS):
        assert (await renew(store, ttl_seconds)).expires_at == clock.now + ttl_seconds * SECOND
