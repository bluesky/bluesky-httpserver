import pytest

from bluesky_httpserver.v2.store import (
    LEASE_TTL_MAX_SECONDS,
    LEASE_TTL_MIN_SECONDS,
    EventType,
    LeaseHeld,
    LeaseRequired,
)

from .conftest import CLIENT, acquire, cancel, reorder, replace, revision_and_event_count, submit

SECOND = 1_000_000


async def renew(store, ttl_seconds, principal="alice", client_id=CLIENT):
    return await store.write(
        lambda tx: tx.renew_lease(principal=principal, client_id=client_id, ttl_seconds=ttl_seconds)
    )


async def release(store, principal, client_id=CLIENT, lease_uid=None):
    await store.write(lambda tx: tx.release_lease(principal=principal, client_id=client_id, lease_uid=lease_uid))


async def override(store, principal, reason, ttl_seconds=60):
    return await store.write(
        lambda tx: tx.override_lease(principal=principal, client_id=CLIENT, reason=reason, ttl_seconds=ttl_seconds)
    )


@pytest.mark.asyncio
async def test_lease_lifecycle(store, clock):
    lease = await store.read(lambda tx: tx.lease())
    assert lease.holder == "alice"
    assert lease.expires_at == clock.now + 3600 * SECOND

    clock.advance(10)
    renewed = await renew(store, 3600)
    assert renewed.lease_uid == lease.lease_uid
    assert renewed.expires_at == clock.now + 3600 * SECOND
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
async def test_clients_of_a_principal_share_its_lease(store, clock):
    # The fixture's lease was taken by alice through CLIENT; an agent acting for her uses it as her own.
    lease = await store.read(lambda tx: tx.lease())
    operation, revision = await submit(store, 0, client_id="agent")
    assert (operation.submitted_by, operation.submitted_via) == ("alice", "agent")

    # A shorter renewal through one client never cuts short a longer one through another.
    clock.advance(10)
    assert (await renew(store, 60, client_id="agent")).expires_at == lease.expires_at
    assert await store.read(lambda tx: tx.lease()) == lease

    # Releasing ends control for every client acting as alice.
    await release(store, "alice", client_id="agent")
    with pytest.raises(LeaseRequired):
        await submit(store, revision)

    events = await store.events_after(0)
    assert [(event.event_type, event.actor_id, event.client_id) for event in events] == [
        (EventType.LEASE_ACQUIRED, "alice", CLIENT),
        (EventType.OPERATION_SUBMITTED, "alice", "agent"),
        (EventType.LEASE_RENEWED, "alice", "agent"),
        (EventType.LEASE_RELEASED, "alice", "agent"),
    ]


@pytest.mark.asyncio
async def test_release_naming_an_ended_grant_leaves_the_newer_one(store):
    # The fixture's grant ends, and an agent acting for alice takes a new one.
    first = await store.read(lambda tx: tx.lease())
    await release(store, "alice", lease_uid=first.lease_uid)
    second = await acquire(store, "alice", client_id="agent")
    before = await revision_and_event_count(store)
    with pytest.raises(LeaseRequired):
        await release(store, "alice", lease_uid=first.lease_uid)
    assert await store.read(lambda tx: tx.lease()) == second
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
    clock.advance(3600 - 1)  # one second of the lease is left, so even the shortest renewal extends it
    for ttl_seconds in (LEASE_TTL_MIN_SECONDS, LEASE_TTL_MAX_SECONDS):
        assert (await renew(store, ttl_seconds)).expires_at == clock.now + ttl_seconds * SECOND
