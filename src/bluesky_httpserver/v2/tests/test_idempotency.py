import pytest

from bluesky_httpserver.v2.store import IdempotencyConflict, IdempotencyScope, StaleRevision, StoredResponse

SCOPE = IdempotencyScope("alice", "POST", "/api/v2/operations", "k1")


class SubmitFn:
    """Submits one operation and returns the response to store; counts its calls."""

    def __init__(self, expected_revision=0, principal="alice"):
        self.expected_revision = expected_revision
        self.principal = principal
        self.calls = 0

    async def __call__(self, tx):
        self.calls += 1
        record, revision = await tx.submit(
            principal=self.principal,
            expected_revision=self.expected_revision,
            operation_id="demo.count",
            operation_version="1",
            parameters={"num": 1},
            descriptor_sha256="0" * 64,
        )
        return StoredResponse(201, {"operation_uid": record.operation_uid}, f'"qrev-{revision}"')


@pytest.mark.asyncio
async def test_repeated_request_is_replayed(store):
    fn = SubmitFn()
    first = await store.write_idempotent(SCOPE, "h1", fn)
    second = await store.write_idempotent(SCOPE, "h1", fn)

    assert not first.replayed
    assert second.replayed
    assert second.response == first.response
    assert fn.calls == 1
    snapshot = await store.queue()
    assert snapshot.revision == 1
    assert len(snapshot.pending) == 1
    assert len(await store.events_after(0)) == 1


@pytest.mark.asyncio
async def test_reused_key_with_different_request_conflicts(store):
    await store.write_idempotent(SCOPE, "h1", SubmitFn())
    fn = SubmitFn(expected_revision=1)
    with pytest.raises(IdempotencyConflict):
        await store.write_idempotent(SCOPE, "h2", fn)
    assert fn.calls == 0
    snapshot = await store.queue()
    assert snapshot.revision == 1
    assert len(snapshot.pending) == 1


@pytest.mark.asyncio
async def test_keys_are_scoped_by_principal(store):
    alice = await store.write_idempotent(SCOPE, "h1", SubmitFn())
    bob_scope = IdempotencyScope("bob", SCOPE.method, SCOPE.path, SCOPE.key)
    bob = await store.write_idempotent(bob_scope, "h1", SubmitFn(expected_revision=1, principal="bob"))
    assert not alice.replayed
    assert not bob.replayed
    assert len((await store.queue()).pending) == 2


@pytest.mark.asyncio
async def test_failed_request_is_not_recorded(store):
    with pytest.raises(StaleRevision):
        await store.write_idempotent(SCOPE, "h1", SubmitFn(expected_revision=5))

    fn = SubmitFn(expected_revision=0)
    retried = await store.write_idempotent(SCOPE, "h1", fn)
    assert not retried.replayed
    assert fn.calls == 1
    assert (await store.write_idempotent(SCOPE, "h1", fn)).replayed
    assert fn.calls == 1
