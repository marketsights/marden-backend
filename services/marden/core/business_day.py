"""The business day: US Eastern (spec 22: snapshots and as-of days are dated in US Eastern).

The database session is UTC, so CURRENT_DATE there (and `date.today()` on a UTC host) is already tomorrow in
New York from 8 pm EDT / 7 pm EST on: a fact whose valid_to is tomorrow would drop out that evening, a day
early. Every "today" cutoff is `business_today()`, as the `fact_current` view's own
((now() AT TIME ZONE 'America/New_York')::date, db/migrations/20261022090000_fact_current_business_day.sql).
marden.readmodels.context re-exports both names with the rest of the business-day helpers.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/New_York")


def business_today() -> date:
    """Snapshots are dated in US Eastern (spec 22: screens rebuilt 5-6 am Eastern)."""
    return datetime.now(BUSINESS_TZ).date()
