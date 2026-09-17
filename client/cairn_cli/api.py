"""Minimal REST client for the Cairn /api/v1 endpoints (stdlib only)."""
import json
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, host: str, token: str | None, timeout: float = 30.0):
        self.host = host.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, *, params=None, body=None):
        url = f"{self.host}/api/v1{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            if exc.code == 401:
                detail = "not authenticated — run `cairn login` (or check CAIRN_TOKEN)"
            raise ApiError(detail or f"HTTP {exc.code}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"cannot reach {self.host}: {exc.reason}") from exc

    # ---- endpoints ----

    def me(self):
        return self._request("GET", "/me")

    def list_tasks(self, **params):
        return self._request("GET", "/tasks", params=params)

    def list_due(self, **params):
        return self._request("GET", "/due", params=params)

    def create_task(self, payload: dict):
        return self._request("POST", "/tasks", body=payload)

    def list_projects(self):
        return self._request("GET", "/projects")
