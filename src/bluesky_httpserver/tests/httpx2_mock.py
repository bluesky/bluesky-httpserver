from dataclasses import dataclass

import httpx2


@dataclass
class Call:
    request: httpx2.Request


class Route:
    def __init__(self, router: "MockRouter", method: str, url: str):
        self._router = router
        self._method = method
        self._url = url

    def mock(self, *, return_value: httpx2.Response) -> None:
        self._router._responses[self._method, self._url] = return_value


class MockRouter:
    def __init__(self):
        self._responses: dict[tuple[str, str], httpx2.Response] = {}
        self.calls: list[Call] = []

    def get(self, url: str) -> Route:
        return Route(self, "GET", url)

    def post(self, url: str) -> Route:
        return Route(self, "POST", url)

    def request(self, method: str, url: str, **kwargs) -> httpx2.Response:
        request = httpx2.Request(method, url, **kwargs)
        self.calls.append(Call(request))
        try:
            response = self._responses[method, str(request.url)]
        except KeyError as exc:
            raise AssertionError(f"Unexpected HTTPX2 request: {method} {request.url}") from exc
        response.request = request
        return response
