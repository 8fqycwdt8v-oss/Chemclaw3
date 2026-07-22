"""The shared HTTP error-detail formatter bounds an upstream body (SEC-6)."""

import httpx

from chemclaw.http import _ERROR_BODY_MAX_CHARS, error_detail


def _response(status: int, text: str) -> httpx.Response:
    """Build an httpx.Response with a text body for formatting."""
    return httpx.Response(status_code=status, text=text)


def test_error_detail_includes_status_and_body() -> None:
    """A short body is reported verbatim alongside the status code."""
    detail = error_detail(_response(500, "boom"))
    assert "500" in detail
    assert "boom" in detail


def test_error_detail_truncates_a_large_body() -> None:
    """A body longer than the cap is truncated with an ellipsis, never streamed whole."""
    detail = error_detail(_response(502, "x" * (_ERROR_BODY_MAX_CHARS + 100)))
    assert "…" in detail
    # The kept body is bounded to the cap (plus the status/reason prefix and ellipsis).
    assert len(detail) < _ERROR_BODY_MAX_CHARS + 100
