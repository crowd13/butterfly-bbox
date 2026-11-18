"""Small stdlib client. Never blindly retry resource-creating POST requests."""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://tasks.yandex.ru/api/v1"


class ApiError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class TasksApi:
    def __init__(self, token=None):
        self.token = (os.environ.get("YANDEX_TASKS_OAUTH", "") if token is None else token).strip()
        if not self.token:
            raise ValueError("Enter a nonempty OAuth token or set YANDEX_TASKS_OAUTH")

    def request(self, method, path, body=None, query=None):
        url = API_ROOT + path
        if path.startswith("/api/new/"):
            url = "https://tasks.yandex.ru" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(
            url,
            payload,
            method=method,
            headers={"Authorization": f"OAuth {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            code = ""
            try:
                details = json.loads(exc.read(16384))
                candidate = details.get("code", "") if isinstance(details, dict) else ""
                if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_.-]{0,79}", candidate):
                    code = f" ({candidate})"
                payload = details.get("payload", {}) if isinstance(details, dict) else {}
                fields = []
                if isinstance(payload, dict) and isinstance(body, dict):
                    for field, issue in payload.items():
                        field_code = issue.get("code", "") if isinstance(issue, dict) else ""
                        if field.split(".", 1)[0] in body and re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_.]{0,119}", field
                        ):
                            valid_code = isinstance(field_code, str) and re.fullmatch(
                                r"[A-Z][A-Z0-9_.-]{0,79}", field_code
                            )
                            fields.append(f"{field}: {field_code}" if valid_code else field)
                if fields:
                    code += " [" + "; ".join(fields[:10]) + "]"
            except (ValueError, OSError):
                pass
            finally:
                exc.close()
            raise ApiError(f"{method} {path}: HTTP {exc.code}{code}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ApiError(f"{method} {path}: uncertain response ({type(exc).__name__})") from None

    def items(self, path, **filters):
        cursor = None
        while True:
            query = {**filters, "sort": "id", "limit": 100}
            if cursor is not None:
                query["id_gt"] = cursor
            page = self.request("GET", path, query=query)
            batch = page["items"]
            yield from batch
            if not page.get("has_more"):
                return
            if not batch or batch[-1]["id"] == cursor:
                raise ApiError(f"Pagination did not advance: {path}")
            cursor = batch[-1]["id"]

    def wait(self, operation):
        if not operation.get("type") or "status" not in operation:
            return operation
        for _ in range(30):
            if operation["status"] == "SUCCESS":
                return operation
            if operation["status"] not in {"PENDING", "RUNNING"}:
                raise ApiError(f"Operation {operation['id']} failed")
            time.sleep(1)
            operation = self.request("GET", f"/operations/{operation['id']}")
        raise ApiError(f"Operation {operation['id']} still running; rerun the command")

    def skill(self, skill_id, user_id, value):
        return self.request(
            "PUT", "/user-skills", {"skill_id": skill_id, "user_id": user_id, "value": value}
        )
