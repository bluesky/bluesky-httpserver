import httpx


def from_uri(uri):
    """
    Create a client connected to a bluesky HTTP server at the given URI.
    """
    return Client(httpx.Client(base_url=uri, event_hooks=EVENT_HOOKS))


class Client:
    """
    Client to a bluesky HTTP server

    Use the function from_uri to build this object. It should generally not be
    necessary to initialize it directly.
    """

    def __init__(self, httpx_client):
        self._client = httpx_client
        self._queue_items = QueueItems(self)

    def __repr__(self):
        # Fetch details with a short timeout and a fallback.
        try:
            status = self.request_json("GET", "/status", timeout=0.2)
        except TimeoutError:
            return f"<{type(self).__name__} (failed to quickly fetch status)>"
        return (
            f"<{type(self).__name__} "
            f"manager_state={status['manager_state']!r} "
            f"re_state={status['re_state']!r}"
            ">"
        )

    def request_json(self, *args, **kwargs):
        "Send HTTP request; raise if errored; return JSON response as dict."
        response = self._client.request(*args, **kwargs)
        handle_error(response)
        data = response.json()
        if not data.pop("success", True):
            raise RequestFailed(data["msg"])
        return data

    def ping(self):
        return self.request_json("GET", "/ping")

    @property
    def status(self):
        return self.request_json("GET", "/status")

    @property
    def items(self):
        return self._queue_items

    @property
    def running_item(self):
        return self.request_json("GET", "/queue/get")["running_item"]

    @property
    def mode(self):
        return self.status["plan_queue_mode"]

    @mode.setter
    def mode(self, value):
        return self.request_json("POST", "/queue/mode/set", json={"mode": value})

    @property
    def loop(self):
        return self.mode["loop"]

    @loop.setter
    def loop(self, value):
        self.mode = {"loop": value}

    def start(self):
        self.request_json("POST", "/queue/start")

    def stop(self):
        self.request_json("POST", "/queue/stop")

    def cancel_stop(self):
        self.request_json("POST", "/queue/stop/cancel")


class QueueItems:
    def __init__(self, client):
        self._client = client

    def __repr__(self):
        return f"<{type(self).__name__}>"

    def __iter__(self):
        yield from [QueueItem(item) for item in self._client.request_json("GET", "/queue/get")["items"]]

    def __len__(self):
        # TODO The server could provide a faster way to query
        # the number of items in the queue.
        return len(self._client.request_json("GET", "/queue/get"))

    def append(self, item):
        ...

    def remove(self, item):
        ...

    def clear(self):
        self._client("POST", "/queue/clear")


class QueueItem:
    def __init__(self, item):
        self._item = item

    def __repr__(self):
        return f"<{type(self).__name__} {self.args!r} {self.kwargs!r}>"

    @property
    def plan(self):
        self.item["name"]

    def args(self):
        self.item["args"]

    def kwargs(self):
        self.item["kwargs"]


def handle_error(response):
    try:
        response.raise_for_status()
    except httpx.RequestError:
        raise  # Nothing to add in this case; just raise it.
    except httpx.HTTPStatusError as exc:
        if response.status_code < 500:
            # Include more detail that httpx does by default.
            message = (
                f"{exc.response.status_code}: "
                f"{exc.response.json()['detail'] if response.content else ''} "
                f"{exc.request.url}"
            )
            raise ClientError(message, exc.request, exc.response) from exc
        else:
            raise


class RequestFailed(Exception):
    pass


class ClientError(httpx.HTTPStatusError):
    def __init__(self, message, request, response):
        super().__init__(message=message, request=request, response=response)


if __debug__:

    import logging
    import os

    # By default, the token in the authentication header is redacted from the logs.
    # Set thie env var to 1 to show it for debugging purposes.
    QUEUE_CLIENT_LOG_AUTH_TOKEN = int(os.getenv("QUEUE_CLIENT_LOG_AUTH_TOKEN", False))

    class LogFormatter(logging.Formatter):
        def __init__(
            self,
            fmt,
            datefmt,
        ):
            super().__init__(datefmt=datefmt)
            self._fmt = fmt

        def format(self, record):
            if isinstance(record.msg, httpx.Request):
                request = record.msg
                record.message = f"-> {request.method} '{request.url}' " + " ".join(
                    f"'{k}:{v}'" for k, v in request.headers.items() if k != "authorization"
                )
                # Handle the authorization header specially.
                # For debugging, it can be useful to show it so that the log message
                # can be copy/pasted and passed to httpie in a shell.
                # But for screen-sharing demos, it should be redacted.
                if QUEUE_CLIENT_LOG_AUTH_TOKEN:
                    if "authorization" in request.headers:
                        record.message += f" 'authorization:{request.headers['authorization']}'"
                else:
                    if "authorization" in request.headers:
                        record.message += " 'authorization:[redacted]'"
            elif isinstance(record.msg, httpx.Response):
                response = record.msg
                request = response.request
                record.message = f"<- {response.status_code} " + " ".join(
                    f"{k}:{v}" for k, v in response.headers.items()
                )
            record.asctime = self.formatTime(record, self.datefmt)

            formatted = self._fmt % record.__dict__

            if record.exc_info and not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                formatted = "{}\n{}".format(formatted.rstrip(), record.exc_text)
            return formatted.replace("\n", "\n    ")

    logger = logging.getLogger(__name__)
    handler = logging.StreamHandler()
    log_format = "%(asctime)s.%(msecs)03d %(message)s"

    handler.setFormatter(LogFormatter(log_format, datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    log = logger.debug

    async def async_log(*args, **kwargs):
        return log(*args, **kwargs)

    EVENT_HOOKS = {"request": [log], "response": [log]}
    ASYNC_EVENT_HOOKS = {"request": [async_log], "response": [async_log]}
else:
    # We take this path when Python is started with -O optimizations.
    ASYNC_EVENT_HOOKS = EVENT_HOOKS = {"request": [], "response": []}
