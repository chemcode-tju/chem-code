from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite


def retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After without assuming the provider has actually recovered."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if isfinite(seconds) else None


class AgentError(Exception):
    """An error whose code and message are safe to expose to a local API client."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        *,
        retry_after: float | None = None,
        cause_code: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
        self.cause_code = cause_code
