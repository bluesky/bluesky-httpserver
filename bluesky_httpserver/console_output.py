import asyncio
import json
import logging
import queue
import uuid
import time as ttime
import inspect

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

        self._callbacks = []
        self._callbacks_async = []

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

    def subscribe(self, cb):
        """
        Add a function or a coroutine to the list of callbacks. The callbacks must accept message as a parameter: cb(msg)
        """
        if inspect.iscoroutinefunction(cb):
            self._callbacks_async.append(cb)
        else:
            self._callbacks.append(cb)

    def unsubscribe(self, cb):
        if inspect.iscoroutinefunction(cb):
            self._callbacks_async.remove(cb)
        else:
            self._callbacks.remove(cb)

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
                for cb in self._callbacks:
                    cb(msg)
                for cb in self._callbacks_async:
                    await cb(msg)
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


class ConsoleOutputStream:
    def __init__(self, *, rm_ref):
        self._queues = {}
        self._queue_max_size = 1000

    @property
    def queues(self):
        return self._queues

    def add_queue(self, key):
        """
        Add a new queue to the dictionary of queues. The key is a reference to the socket for
        for connection with the client.
        """
        queue = asyncio.Queue(maxsize=self._queue_max_size)
        self._queues[key] = queue
        return queue

    def remove_queue(self, key):
        """
        Remove the queue identified by the key from the dictionary of queues.
        """
        if key in self._queues:
            del self._queues[key]

    async def add_message(self, msg):
        msg_json = json.dumps(msg)
        for q in self._queues.values():
            # Protect from overflow. It's ok to discard old messages.
            if q.full():
                q.get_nowait()
            await q.put(msg_json)

    def start(self):
        pass

    async def stop(self):
        pass


class SystemInfoStream:
    def __init__(self, *, rm_ref):
        self._RM = rm_ref
        self._queues = {}
        self._background_task = None
        self._background_task_running = False
        self._background_task_stopped = asyncio.Event()
        self._background_task_stopped.set()
        self._num = 0
        self._queue_max_size = 1000

    @property
    def background_task_running(self):
        return self._background_task_running

    @property
    def queues(self):
        return self._queues

    def add_queue(self, key):
        """
        Add a new queue to the dictionary of queues. The key is a reference to the socket for
        for connection with the client.
        """
        queue = asyncio.Queue(maxsize=self._queue_max_size)
        self._queues[key] = queue
        return queue

    def remove_queue(self, key):
        """
        Remove the queue identified by the key from the dictionary of queues.
        """
        if key in self._queues:
            del self._queues[key]

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
                msg = await self._RM.system_info_monitor.next_msg(timeout=0.5)
                # Discard all messages except status messages
                if isinstance(msg, dict) and "msg" in msg and "status" in msg["msg"]:
                    msg_json = json.dumps(msg)
                    # self._add_message(msg=msg)
                    for q in self._queues.values():
                        # Protect from overflow. It's ok to discard old messages.
                        if q.full():
                            q.get_nowait()
                        await q.put(msg_json)
            except self._RM.RequestTimeoutError:
                pass

            # await asyncio.sleep(1)
            # try:
            #     # msg = await self._RM.console_monitor.next_msg(timeout=0.5)
            #     # self._add_message(msg=msg)
            #     msg = f"Message {self._num}\n"
            #     print(f"msg={msg.strip()}")  ##
            #     self._num += 1
            #     for q in self._queues.values():
            #         # Protect from overflow. It's ok to discard old messages.
            #         if q.full():
            #             q.get_nowait()
            #         await q.put(msg)
            # except self._RM.RequestTimeoutError:
            #     pass
        self._background_task_stopped.set()

    def start(self):
        self._RM.system_info_monitor.enable()
        self._start_background_task()

    async def stop(self):
        await self._stop_background_task()
        await self._RM.system_info_monitor.disable_wait()

