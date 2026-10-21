"""`python -m marden seed all`: every seed loader of marden_data_2026-10-20.zip, in dependency order.

    cd services && uv run python -m marden seed all [--write-docs]

Order: ``sites`` (sites, parties, obligations) -> ``instruments`` (needs sites and entities) ->
``holdings`` (links lines to instruments) -> ``facilities`` (campuses, plants, site links, events on sites and
instruments) -> ``cases`` (per-site research on all of the above). Each loader is idempotent and records its
own ``source_run`` of ``seed_marden_data``, so running ``seed all`` twice gives the same row counts. The
legacy price loaders (``legacy_trace``, ``legacy_frankfurt``, ``legacy_yahoo``) are not part of the seed
load: docs/methods/price-sources.md sections 2-4 keep finra.org, Börse Frankfurt and Yahoo values out of it.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from psycopg import sql

from marden import cli
from marden.core import db
from marden.core.config import REPO_ROOT
from marden.seed import cases, facilities, holdings, instruments, sites
from marden.seed.run_all_gaps import write_gaps

Handler = Callable[[argparse.Namespace], int | None]

ORDER: tuple[tuple[str, Handler], ...] = (
    ("sites", sites.seed_sites),
    ("instruments", instruments.run_cli),
    ("holdings", holdings.run_cli),
    ("facilities", facilities.seed_facilities),
    ("cases", cases.seed_cases),
)
TABLES = (
    "site", "chain_party", "obligation", "entity", "instrument", "holding", "price_obs", "campus", "building",
    "power_plant", "power_unit", "plant_serves", "site_link", "event", "model_output", "review_item", "document",
)


def table_counts() -> dict[str, int]:
    with db.connection() as conn:
        out = {t: conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(t))).fetchone()["n"]
               for t in TABLES}
        rows = conn.execute(
            "SELECT producer, count(*) AS n FROM fact WHERE producer LIKE 'seed:%' GROUP BY 1 ORDER BY 1"
        ).fetchall()
    out.update({f"fact[{r['producer']}]": r["n"] for r in rows})
    return out


def _configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--write-docs", action="store_true",
                   help="also regenerate docs/seed-data/v1-instruments.md, unpriced.md and gaps.md")


@cli.action("seed", "all", help="Run every seed loader in order: sites, instruments, holdings, facilities, cases",
            configure=_configure)
def seed_all(args: argparse.Namespace) -> int:
    loader_args = argparse.Namespace(cases=None, write_docs=getattr(args, "write_docs", False))
    for name, handler in ORDER:
        started = time.monotonic()
        print(f"== seed {name}", flush=True)
        code = int(handler(loader_args) or 0)
        print(f"== seed {name}: exit {code} in {time.monotonic() - started:.1f}s", flush=True)
        if code:
            return code
    for table, n in table_counts().items():
        print(f"{table}: {n}")
    if loader_args.write_docs:
        print(f"wrote {write_gaps(REPO_ROOT / 'docs' / 'seed-data')}")
    return 0
