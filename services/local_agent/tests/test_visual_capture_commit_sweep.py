"""Synthetic pixels, real transactions; deterministic maintenance/precommit interleaving."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local
from test_visual_capture_producer import local_client, begin, capture, revise


def test_background_sweep_cannot_drop_staged_crop_before_revision_commit(tmp_path, monkeypatch):
    with local_client(tmp_path) as client:
        sid, root = begin(client)
        artifact, _ = capture(client, sid)
        producer = client.app.state.visual_capture
        original_lock = producer.host._stage_lock
        staged, release_commit, sweep_attempted = Event(), Event(), Event()
        in_sweep = local()
        acquired_before_commit = []

        class ObservedLock:
            """Delegate the original lock; expose one real acquisition for scheduling."""
            def __enter__(self):
                if getattr(in_sweep, 'active', False) and not acquired_before_commit:
                    acquired = original_lock.acquire(blocking=False)
                    acquired_before_commit.append(acquired)
                    sweep_attempted.set()
                    if not acquired:
                        original_lock.acquire()
                else:
                    original_lock.acquire()
                return self

            def __exit__(self, *_):
                original_lock.release()

        monkeypatch.setattr(producer.host, '_stage_lock', ObservedLock())
        original_stage = producer.host.stage_prepared

        def before_commit(*args, **kwargs):
            identity = original_stage(*args, **kwargs)
            staged.set()
            assert release_commit.wait(10), 'test did not release the original transaction'
            return identity

        monkeypatch.setattr(producer.host, 'stage_prepared', before_commit)

        def sweep():
            in_sweep.active = True
            producer.host.sweep_prepared()

        with ThreadPoolExecutor(max_workers=2) as workers:
            revision = workers.submit(revise, client, sid, artifact)
            try:
                assert staged.wait(10), 'the real sealed gate did not stage the synthetic crop'
                maintenance = workers.submit(sweep)
                assert sweep_attempted.wait(10)
                # If maintenance can acquire the lock, let its original DB read
                # complete before the writer commits. Otherwise commit releases
                # the barrier and the exact same sweep checks committed state.
                if acquired_before_commit[0]:
                    maintenance.result(timeout=10)
            finally:
                release_commit.set()
            response = revision.result(timeout=10)
            maintenance.result(timeout=10)
        assert response.status_code == 200, response.text
        assert response.json()['visual_capture']['items']
        assert producer.host._staged, 'maintenance removed bytes while their source transaction was uncommitted'
        assert client.post(root + '/stop', json={'client_action_id': 'sweep-after-commit-stop'}).status_code == 200
        assert not producer.host._staged and not producer._pending
