from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class MesHttpResult:
    transport_ok: bool
    status_code: int | None
    elapsed_ms: int
    response_text: str = ""
    response_json: Any = None
    error_kind: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _response_charset(headers: Any) -> str:
    try:
        return headers.get_content_charset() or "utf-8"
    except Exception:
        return "utf-8"


def _read_response(response: Any) -> tuple[str, Any]:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError(f"MES response exceeds {MAX_RESPONSE_BYTES} bytes")
    text = raw.decode(_response_charset(response.headers), errors="replace")
    if not text.strip():
        return text, None
    try:
        return text, json.loads(text)
    except json.JSONDecodeError:
        return text, None


class MesHttpClient:
    def post_json(self, url: str, payload: dict[str, Any], timeout_s: float = 5.0) -> MesHttpResult:
        started = time.monotonic()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url.strip(),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, float(timeout_s))) as response:
                text, parsed = _read_response(response)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                status_code = int(getattr(response, "status", response.getcode()))
                return MesHttpResult(
                    transport_ok=200 <= status_code < 300,
                    status_code=status_code,
                    elapsed_ms=elapsed_ms,
                    response_text=text,
                    response_json=parsed,
                    error_kind="" if 200 <= status_code < 300 else "http",
                    message=f"HTTP {status_code}",
                )
        except urllib.error.HTTPError as exc:
            try:
                text, parsed = _read_response(exc)
            except Exception:
                text, parsed = str(exc), None
            return MesHttpResult(
                transport_ok=False,
                status_code=int(exc.code),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                response_text=text,
                response_json=parsed,
                error_kind="http",
                message=f"HTTP {exc.code}",
            )
        except (TimeoutError, socket.timeout) as exc:
            return MesHttpResult(
                transport_ok=False,
                status_code=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_kind="timeout",
                message=str(exc) or "MES request timeout",
            )
        except urllib.error.URLError as exc:
            reason = exc.reason
            error_kind = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network"
            return MesHttpResult(
                transport_ok=False,
                status_code=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_kind=error_kind,
                message=str(reason or exc),
            )
        except (TypeError, ValueError) as exc:
            return MesHttpResult(
                transport_ok=False,
                status_code=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_kind="response",
                message=str(exc),
            )
        except Exception as exc:
            return MesHttpResult(
                transport_ok=False,
                status_code=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_kind="unexpected",
                message=str(exc),
            )
