from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(
    base_url: str,
    session_token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-HSC-Session": session_token,
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    except URLError as error:
        raise AssertionError(f"Local agent request failed: {error.reason}") from error


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def assert_locked(base_url: str, session_token: str, correct_pin: str) -> int:
    status_code, status = request_json(base_url, session_token, "GET", "/v1/parent/status")
    require(status_code == 200, f"Parent status returned HTTP {status_code}.")
    require(status.get("configured") is True, "Parent PIN was not configured.")
    locked_seconds = status.get("locked_seconds")
    require(
        isinstance(locked_seconds, int) and 1 <= locked_seconds <= 300,
        f"Expected an active persisted lock, got locked_seconds={locked_seconds!r}.",
    )

    unlock_code, unlock = request_json(
        base_url,
        session_token,
        "POST",
        "/v1/parent/unlock",
        {"pin": correct_pin},
    )
    require(unlock_code == 429, f"Correct PIN bypassed active lock with HTTP {unlock_code}.")
    require(unlock.get("code") == "PARENT_LOCKED", "Active lock returned the wrong error code.")
    detail_seconds = unlock.get("details", {}).get("locked_seconds")
    require(
        isinstance(detail_seconds, int) and 1 <= detail_seconds <= locked_seconds,
        f"Lock response did not expose a valid remaining duration: {detail_seconds!r}.",
    )
    return detail_seconds


def create_lock(base_url: str, session_token: str, correct_pin: str, wrong_pin: str) -> int:
    expected_remaining = [4, 3, 2, 1]
    for index in range(5):
        response_code, response = request_json(
            base_url,
            session_token,
            "POST",
            "/v1/parent/unlock",
            {"pin": wrong_pin},
        )
        expected_code = 401 if index < 4 else 429
        require(
            response_code == expected_code,
            f"Wrong PIN attempt {index + 1} returned HTTP {response_code}, expected {expected_code}.",
        )
        if index < 4:
            require(response.get("code") == "INVALID_PARENT_PIN", "Wrong PIN returned the wrong error code.")
            require(
                f"还可尝试{expected_remaining[index]}次" in response.get("message", ""),
                f"Wrong PIN attempt {index + 1} reported an incorrect remaining budget.",
            )
        else:
            require(response.get("code") == "PARENT_LOCKED", "Fifth wrong PIN did not start lockout.")
            require(
                response.get("details", {}).get("locked_seconds") == 300,
                "Fifth wrong PIN did not return the full 300-second lock duration.",
            )
    return assert_locked(base_url, session_token, correct_pin)


def main() -> int:
    parser = argparse.ArgumentParser(description="Black-box parent PIN lockout verification.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--session-token", required=True)
    parser.add_argument("--correct-pin", default="2468")
    parser.add_argument("--wrong-pin", default="1111")
    parser.add_argument("--phase", choices=("lock", "assert-locked"), required=True)
    args = parser.parse_args()

    require(args.correct_pin != args.wrong_pin, "Correct and wrong PIN must differ.")
    remaining = (
        create_lock(args.base_url, args.session_token, args.correct_pin, args.wrong_pin)
        if args.phase == "lock"
        else assert_locked(args.base_url, args.session_token, args.correct_pin)
    )
    print(f"Parent PIN lockout black-box phase '{args.phase}' passed ({remaining}s remaining).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, ValueError, json.JSONDecodeError) as error:
        print(f"Parent PIN lockout black-box verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
