"""Simple in-memory rate limiter for FastAPI endpoints.

Uses a sliding-window approach with configurable limits.
For production, consider using Redis-based rate limiting.
"""

import time
from collections import defaultdict
from threading import Lock
from typing import Dict, Tuple

from fastapi import HTTPException, Request, status


class RateLimiter:
    """In-memory rate limiter using a sliding window."""

    def __init__(self, max_requests: int = 5, window_seconds: int = 60, endpoint_name: str = ""):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.endpoint_name = endpoint_name
        self._clients: Dict[str, list] = defaultdict(list)
        self._lock = Lock()

    def _clean_window(self, client_id: str, now: float) -> None:
        cutoff = now - self.window_seconds
        self._clients[client_id] = [
            ts for ts in self._clients[client_id] if ts > cutoff
        ]

    def is_rate_limited(self, client_id: str) -> Tuple[bool, int]:
        now = time.time()
        with self._lock:
            self._clean_window(client_id, now)
            request_count = len(self._clients[client_id])
            remaining = max(0, self.max_requests - request_count)

            if request_count >= self.max_requests:
                return True, 0

            self._clients[client_id].append(now)
            return False, remaining - 1

    async def __call__(self, request: Request):
        """FastAPI dependency callable."""
        client_id = _get_client_id(request)
        is_limited, _ = self.is_rate_limited(client_id)

        if is_limited:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Too many requests{f' to {self.endpoint_name}' if self.endpoint_name else ''}. "
                    f"Please try again later."
                ),
                headers={"Retry-After": str(self.window_seconds)},
            )


def _get_client_id(request: Request) -> str:
    """Extract a client identifier from the request."""
    xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip") or request.headers.get("X-Real-Ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"


# Pre-configured rate limiter instances
auth_rate_limiter = RateLimiter(max_requests=10, window_seconds=60, endpoint_name="login/register")
sensitive_rate_limiter = RateLimiter(max_requests=5, window_seconds=60, endpoint_name="password reset")
general_rate_limiter = RateLimiter(max_requests=60, window_seconds=60)
