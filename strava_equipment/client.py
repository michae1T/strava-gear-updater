"""Minimal Strava API client."""

from __future__ import annotations

import time

import requests

API_BASE = "https://www.strava.com/api/v3"


class StravaClient:
    def __init__(self, access_token: str, max_retries: int = 0, retry_wait: float = 60.0):
        """
        max_retries: how many times to retry a request after a 429 (rate limit).
        retry_wait:  base seconds to wait before retrying; grows exponentially
                     and respects a Retry-After header when Strava sends one.
        """
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        self._max_retries = max_retries
        self._retry_wait = retry_wait

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Issue a request, retrying on 429 up to max_retries.

        Strava's 15-minute window resets on the clock (:00/:15/:30/:45) and it
        sends no Retry-After header, so on a 429 we wait until the next boundary
        rather than using a fixed backoff. If the *daily* cap is exhausted we
        stop retrying immediately, since it won't reset until midnight UTC.
        """
        attempt = 0
        while True:
            resp = self._session.request(method, f"{API_BASE}{path}", timeout=30, **kwargs)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp

            usage = self._format_usage(resp)
            if self._daily_exhausted(resp):
                raise RuntimeError(
                    "Strava daily rate limit reached "
                    f"({usage}). It resets at midnight UTC — try again later."
                )
            if attempt >= self._max_retries:
                resp.raise_for_status()  # raises the 429 to the caller

            wait = self._seconds_to_window_reset()
            print(f"    rate limited ({usage}); waiting {wait:.0f}s for the "
                  f"15-min window to reset, retry {attempt + 1}/{self._max_retries}...")
            time.sleep(wait)
            attempt += 1

    @staticmethod
    def _parse_pair(value: str | None) -> tuple[int, int] | None:
        """Parse a '15min,daily' header into ints, or None if absent/malformed."""
        if not value or "," not in value:
            return None
        try:
            short, daily = value.split(",", 1)
            return int(short), int(daily)
        except ValueError:
            return None

    def _daily_exhausted(self, resp: requests.Response) -> bool:
        """True if either the overall or read daily quota is used up."""
        for usage_h, limit_h in (
            ("X-RateLimit-Usage", "X-RateLimit-Limit"),
            ("X-ReadRateLimit-Usage", "X-ReadRateLimit-Limit"),
        ):
            usage = self._parse_pair(resp.headers.get(usage_h))
            limit = self._parse_pair(resp.headers.get(limit_h))
            if usage and limit and usage[1] >= limit[1]:
                return True
        return False

    def _format_usage(self, resp: requests.Response) -> str:
        """Human-readable usage/limit summary from the response headers."""
        usage = resp.headers.get("X-RateLimit-Usage")
        limit = resp.headers.get("X-RateLimit-Limit")
        if usage and limit:
            return f"usage {usage} of {limit} [15min,daily]"
        return "no usage headers returned"

    def _seconds_to_window_reset(self) -> float:
        """Seconds until the next :00/:15/:30/:45 boundary, plus a small buffer.

        Never waits less than retry_wait, so a request landing just before a
        boundary doesn't retry into the same nearly-full window.
        """
        into_window = time.time() % 900  # 900s = 15 minutes
        return max(900 - into_window + 5, self._retry_wait)

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        return self._request("GET", path, params=params).json()

    def get_athlete(self) -> dict:
        """The detailed authenticated athlete, including `bikes` and `shoes`."""
        return self._get("/athlete")

    def get_gear(self, gear_id: str) -> dict:
        """Detailed representation of a single piece of gear."""
        return self._get(f"/gear/{gear_id}")

    def rate_limit_status(self) -> dict:
        """Report current rate-limit usage via one lightweight read.

        Returns {'overall': {...}, 'read': {...}}, each with 15-minute and
        daily usage/limit pairs (or None if a header was absent). Costs one
        request, which is itself reflected in the returned numbers.
        """
        resp = self._request("GET", "/athlete")
        out = {}
        for key, usage_h, limit_h in (
            ("overall", "X-RateLimit-Usage", "X-RateLimit-Limit"),
            ("read", "X-ReadRateLimit-Usage", "X-ReadRateLimit-Limit"),
        ):
            out[key] = {
                "usage": self._parse_pair(resp.headers.get(usage_h)),
                "limit": self._parse_pair(resp.headers.get(limit_h)),
            }
        return out

    def iter_activities(self, per_page: int = 200):
        """Yield every activity in the athlete's history, newest first."""
        page = 1
        while True:
            batch = self._get(
                "/athlete/activities",
                params={"per_page": per_page, "page": page},
            )
            if not batch:
                return
            yield from batch
            page += 1

    def update_activity_gear(self, activity_id: int, gear_id: str) -> dict:
        """Assign `gear_id` to an activity. Requires the `activity:write` scope."""
        return self._request(
            "PUT", f"/activities/{activity_id}", data={"gear_id": gear_id}
        ).json()
