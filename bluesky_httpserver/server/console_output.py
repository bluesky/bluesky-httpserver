import threading
import json
from bluesky_queueserver import ReceiveConsoleOutput
import queue
from starlette.responses import StreamingResponse


# The set of queues for console output
queues_console_output = set()


class FetchPublishedConsoleOutput(threading.Thread):
    """
    The class implements the code for fetching console output data published by Queue Server
    and adding the messages to the queues in the set (see parameter ``queues_set``). The
    code is executed in a separate thread.

    Examples
    --------
    .. code-block:: python
        # Instantiate the class and start the thread
        gen_data = FetchPublishedConsoleOutput()
        gen_data.start()

    Parameters
    ----------
    zmq_addr : str or None
        0MQ address of the Queue Server socket where the console output messages are published.
        E.g. ``tcp://localhost:60625``. If ``None``, then default socket address is used.
    **kwargs
        Passed to ``threading.Thread``.
    """

    def __init__(self, *, zmq_addr=None, **kwargs):
        global queues_console_output

        kwargs.update({"name": "QServer HTTP Console Output Streaming", "daemon": True})
        super().__init__(**kwargs)
        self._exit = False
        self._queues_set = queues_console_output
        self._rco = ReceiveConsoleOutput(zmq_subscribe_addr=zmq_addr)

    def run(self):
        while True:
            try:
                message = self._rco.recv(timeout=500)
                for q in self._queues_set.copy():
                    # Consume one message if the queue is full. Setting the maximum
                    #   queue size may save from memory leaks in case queue is not
                    #   removed from the set due to a bug.
                    if q.full():
                        q.get()

                    q.put(message)

            except TimeoutError:
                # Timeout does not mean communication error!!!
                pass

            if self._exit:
                break

    def stop(self):
        self._exit = True


class ConsoleOutputEventStream:
    def __init__(self, *, queue_maxsize=1000):
        global queues_console_output

        self._queues_set = queues_console_output

        self._local_queue = queue.Queue(maxsize=queue_maxsize)
        self._queues_set.add(self._local_queue)

    def __call__(self):
        while True:
            try:
                message = self._local_queue.get(timeout=0.1)
                yield json.dumps(message)
            except queue.Empty:
                pass

    def __del__(self):
        self._queues_set.remove(self._local_queue)


class StreamingResponseFromClass(StreamingResponse):
    def __init__(self, content_class, *args, **kwargs):
        self._content = content_class
        super().__init__(content=content_class(), *args, **kwargs)

    def __del__(self):
        del self._content
