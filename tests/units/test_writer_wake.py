"""Closing a unit's port must not hold the reactor: the writer thread waits
on its queue for up to 0.5 s, so it is woken before the join. A wake its
thread never took (it had ended) does not stay behind as a request.
"""
import importlib
import queue
import threading
import time

from . import harness as H


def _rig():
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')

    class Ace(object):
        _wake_writer = A.MultiAce._wake_writer
        _drop_writer_queue = A.MultiAce._drop_writer_queue

        def __init__(self):
            self._writer_queues = {}
            self._writer_threads = {}
    return A, Ace()


def test_waiting_writer_ends_at_once():
    A, ace = _rig()
    q = queue.Queue()
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                q.get(timeout=0.5)
            except queue.Empty:
                continue
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    time.sleep(0.05)
    ace._writer_queues[0] = q
    ace._writer_threads[0] = t
    stop.set()
    t0 = time.monotonic()
    ace._wake_writer(0)
    t.join(timeout=A.THREAD_JOIN_TIMEOUT)
    assert not t.is_alive()
    assert time.monotonic() - t0 < 0.2
    assert A.THREAD_JOIN_TIMEOUT <= 0.1


def test_dead_writer_gets_no_wake():
    A, ace = _rig()
    q = queue.Queue()
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    ace._writer_queues[0] = q
    ace._writer_threads[0] = t
    ace._wake_writer(0)
    assert q.empty()


def test_untaken_wake_is_removed_requests_stay():
    A, ace = _rig()
    q = queue.Queue()
    q.put({'method': 'get_status'})
    q.put(None)
    ace._writer_queues[0] = q
    ace._drop_writer_queue(0)
    assert 0 not in ace._writer_queues
    assert list(q.queue) == [{'method': 'get_status'}]
