import asyncio
import json
import logging
import queue
import uuid

from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class CollectPublishedConsoleOutput:
    """
    The class implements the code for collecting messages with console output data published by
    RE Manager and adding the messages to the queues in the set. The code is executed in a
    separate thread.

    Examples
    --------
    .. code-block:: python
        from bluesky_queueserver_api.zmq.aio import REManagerAPI
        RM = REManagerAPI()

        # Instantiate the class and start the thread
        gen_data = CollectPublishedConsoleOutput(rm_api=RM)
        gen_data.start()
        ...
        q = queue.Queue(maxsize=queue_maxsize)
        gen_data.queues_set.add(self._local_queue)
        ...
        get_data.stop()

    Parameters
    ----------
    rm_ref : bluesky_queueserver_api.REManagerAPI
        Reference to configured REManagerAPI object (0MQ, asyncio)
    """

    def __init__(self, *, rm_ref):
        self._RM = rm_ref
        self._queues_set = set()

        self._msg_buffer_max = 2000
        self._msg_uid_buffer = []
        self._msg_buffer = []
        self._last_msg_uid = str(uuid.uuid4())

        self._background_task = None
        self._background_task_running = False
        self._background_task_stopped = asyncio.Event()
        self._background_task_stopped.set()

    @property
    def queues_set(self):
        """
        Get reference to the set of queues. Received messages are added to each queue in
        in the set. Each independent consumer of messages is expected to add queue to the set.
        This class does not distinguish between consumers and treat all queues identically.
        """
        return self._queues_set

    @property
    def text_buffer_uid(self):
        return self._RM.console_monitor.text_uid

    async def get_text_buffer(self, n_lines):
        return await self._RM.console_monitor.text(n_lines)

    def get_new_msgs(self, last_msg_uid):
        msg_list = []
        try:
            if last_msg_uid == "ALL":
                last_ind = -1  # Return all messages saved in the buffer
            else:
                last_ind = self._msg_uid_buffer.index(last_msg_uid)
            msg_list = self._msg_buffer[last_ind + 1 :]
        except ValueError:
            pass
        return {"last_msg_uid": self._last_msg_uid, "console_output_msgs": msg_list}

    def _start_background_task(self):
        if not self._background_task_running:
            self._background_task = asyncio.create_task(self._load_msgs_task())

    async def _stop_background_task(self):
        self._background_task_running = False
        await self._background_task_stopped.wait()

    async def _load_msgs_task(self):
        self._background_task_stopped.clear()
        self._background_task_running = True
        while self._background_task_running:
            try:
                msg = await self._RM.console_monitor.next_msg(timeout=0.5)
                self._add_message(msg=msg)
            except self._RM.RequestTimeoutError:
                pass
        self._background_task_stopped.set()

    def _add_to_msg_buffer(self, msg):
        uid = str(uuid.uuid4())
        self._msg_buffer.append(msg)
        self._msg_uid_buffer.append(uid)
        self._last_msg_uid = uid

        # Remove extra messages
        while len(self._msg_buffer) > self._msg_buffer_max:
            self._msg_buffer.pop(0)
            self._msg_uid_buffer.pop(0)

    def _add_message(self, msg):
        try:
            for q in self._queues_set.copy():
                # Consume one message if the queue is full. Setting the maximum
                #   queue size may save from memory leaks in case queue is not
                #   removed from the set due to a bug.
                if q.full():
                    q.get()

                q.put(msg)

            # Always add to msg buffer
            self._add_to_msg_buffer(msg)

        except Exception as ex:
            logger.exception("Exception occurred while adding console output message to queues: %s", str(ex))

    def start(self):
        """
        Start collection of messages. Must be called from the loop!!!
        """
        self._RM.console_monitor.enable()
        self._start_background_task()

    async def stop(self):
        """
        Stop collection of messages
        """
        await self._stop_background_task()
        await self._RM.console_monitor.disable_wait()


class ConsoleOutputEventStream:
    def __init__(self, *, queues_set, queue_maxsize=1000):
        self._queues_set = queues_set

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
