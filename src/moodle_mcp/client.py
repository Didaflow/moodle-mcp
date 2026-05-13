"""Moodle Web Services REST client.

Wraps the Moodle external functions API. All calls go to
{MOODLE_URL}/webservice/rest/server.php with the user's token, the desired
wsfunction, and moodlewsrestformat=json.

Token is read from MOODLE_TOKEN env var; base URL from MOODLE_URL (the bare
site URL, e.g. https://moodle.example.org — the /webservice/... suffix is
appended internally so the same URL can be reused for other endpoints).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0
WS_ENDPOINT = "/webservice/rest/server.php"


class MoodleConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class MoodleAPIError(RuntimeError):
    """Raised when the Moodle API returns an exception envelope."""

    def __init__(self, errorcode: str, message: str, debuginfo: str | None = None):
        self.errorcode = errorcode
        self.message = message
        self.debuginfo = debuginfo
        super().__init__(f"[{errorcode}] {message}")


class MoodleClient:
    """Thin async wrapper over the Moodle REST Web Services endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        base_url = base_url or os.environ.get("MOODLE_URL")
        token = token or os.environ.get("MOODLE_TOKEN")

        if not base_url:
            raise MoodleConfigError(
                "MOODLE_URL is not set. Provide the Moodle site URL "
                "(e.g. https://moodle.example.org) via env var or constructor."
            )
        if not token:
            raise MoodleConfigError(
                "MOODLE_TOKEN is not set. Generate a Web Services token from "
                "{site}/user/managetoken.php and export MOODLE_TOKEN."
            )

        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._endpoint = f"{self.base_url}{WS_ENDPOINT}"

    async def call(
        self,
        wsfunction: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Invoke a Moodle external function.

        Moodle expects form-encoded params, including array-style keys like
        `courseids[0]=1&courseids[1]=2`. httpx serializes lists in `data` that
        way when we pre-flatten them, so we do that here.

        Returns the decoded JSON payload. If Moodle returns an exception
        envelope (dict with `exception` key) raises MoodleAPIError.
        """
        form: dict[str, Any] = {
            "wstoken": self.token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
        }
        if params:
            form.update(_flatten_params(params))

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self._endpoint, data=form)
            resp.raise_for_status()
            payload = resp.json()

        if isinstance(payload, dict) and payload.get("exception"):
            raise MoodleAPIError(
                errorcode=payload.get("errorcode", "unknown"),
                message=payload.get("message", "Unknown error"),
                debuginfo=payload.get("debuginfo"),
            )
        return payload

    async def download_file_bytes(self, file_url: str) -> bytes:
        """Download a Moodle file via pluginfile.php with the WS token.

        Appends `?token=...` (or `&token=...` if URL already has a query) and
        follows redirects. Returns the raw response body.

        Raises:
            MoodleAPIError: on any non-2xx status, wrapping the status code.
        """
        sep = "&" if "?" in file_url else "?"
        url = f"{file_url}{sep}token={self.token}"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                raise MoodleAPIError(
                    errorcode=f"http_{resp.status_code}",
                    message=f"File download failed: HTTP {resp.status_code}",
                    debuginfo=resp.text[:500],
                )
            return resp.content


def _flatten_params(params: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts/lists into Moodle's PHP-style form keys.

    Examples
    --------
    {"courseids": [1, 2]}            -> {"courseids[0]": 1, "courseids[1]": 2}
    {"options": {"userid": 5}}       -> {"options[userid]": 5}
    {"criteria": [{"key": "id",      -> {"criteria[0][key]": "id",
                   "value": "3"}]}       "criteria[0][value]": "3"}
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        full_key = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten_params(value, full_key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                item_key = f"{full_key}[{i}]"
                if isinstance(item, dict):
                    out.update(_flatten_params(item, item_key))
                else:
                    out[item_key] = item
        elif value is None:
            continue
        else:
            out[full_key] = value
    return out


def format_error(e: Exception) -> str:
    """Translate exceptions into actionable error strings for the LLM."""
    if isinstance(e, MoodleAPIError):
        hints = {
            "invalidtoken": (
                "The MOODLE_TOKEN is wrong or has expired. "
                "Regenerate it from /user/managetoken.php and update MOODLE_TOKEN."
            ),
            "couldnotauthenticate": (
                "Could not authenticate: the token is invalid OR the external service "
                "is disabled. Verify the token and ask a Moodle admin to confirm the "
                "Web Services service is enabled in Site administration > Server > Web services."
            ),
            "accessexception": (
                "Access denied: the token's user is not in the authorized users of the "
                "external service. Ask a Moodle admin to add the user to the service's "
                "authorized users list."
            ),
            "nopermissions": (
                "No permission: the token's user lacks the role-level capability required "
                "by this function. This is a role/capability issue (different from "
                "accessexception, which is a service-membership issue). Ask a Moodle admin "
                "to grant the required capability to the user's role."
            ),
            "servicerequireslogin": (
                "This Web Services function is not added to the external service the "
                "token belongs to. Ask a Moodle admin to add it in Site administration > "
                "Server > Web services > External services > Functions."
            ),
            "webservice_function_not_found_in_service": (
                "This Web Services function is not enabled for the token's external service. "
                "Ask a Moodle admin to add it to the service in Site administration > Server > Web services."
            ),
            "invalidrecord": "Resource not found. Check the ID is correct.",
            "invalidparameter": (
                "One or more parameters were invalid: wrong param name, wrong type, or "
                "missing required field. Check the function's expected signature."
            ),
        }
        hint = hints.get(e.errorcode, "")
        return f"Moodle API error [{e.errorcode}]: {e.message}" + (f" — {hint}" if hint else "")
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP error {e.response.status_code}: {e.response.text[:200]}"
    if isinstance(e, httpx.TimeoutException):
        return "Request timed out. The Moodle instance may be slow or unreachable."
    if isinstance(e, MoodleConfigError):
        return f"Configuration error: {e}"
    return f"Unexpected error: {type(e).__name__}: {e}"
