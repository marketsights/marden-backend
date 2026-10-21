"""EIA-861 balancing authority table -> EIA BA ids, codes, names and states on `grid_region` rows 'ba:<code>'.

Source: the annual EIA-861 zip, https://www.eia.gov/electricity/data/eia861/zip/f861<year>.zip, member
Balancing_Authority_<year>.xlsx (Data Year, BA ID, BA Code, State, Balancing Authority Name; one row per BA and
state). The BA ID is the EIA utility id that HIFLD's Control Areas layer carries as `source_ID`, so this table
is the official crosswalk from those boundaries to EIA-930 codes. It also covers BAs EIA-930 omits (Alaska,
Hawaii). The newest year on the server is used (current year - 1, else current year - 2).

Only the balancing authority table is read; nothing else in the zip is parsed here (EIA-861 sales, rates and
service territory tables belong to their own collectors). TERMS: EIA public domain, as `eia930_ref`.
Cadence: monthly check; EIA publishes the final annual release each autumn. `python -m marden collect eia861_ba`.
"""

from __future__ import annotations

import argparse
import io
import zipfile
from datetime import date
from typing import Any

from marden import cli
from marden.collectors import grid_registry
from marden.collectors.eia930_ref import TERMS_NOTE, document_as_of
from marden.collectors.grid_store import Region, UpsertStats, upsert
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.core import db
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

SOURCE_ID = "eia861_ba"
CRON_UTC = "19 11 2 * *"  # 2nd of each month, 11:19 UTC (after eia930_ref)
SOURCE = declare_source(SOURCE_ID, "EIA-861 balancing authority table", "feed",
                        "https://www.eia.gov/electricity/data/eia861/", 30 * 24 * 60, TERMS_NOTE)
HEADER = ("Data Year", "BA ID", "BA Code", "State", "Balancing Authority Name")


def zip_url(year: int) -> str:
    return f"https://www.eia.gov/electricity/data/eia861/zip/f861{year}.zip"


def member_name(year: int) -> str:
    return f"Balancing_Authority_{year}.xlsx"


def parse_xlsx(data: bytes) -> dict[str, dict[str, Any]]:
    """code -> {year, ba_ids, names, states} from Balancing_Authority_<year>.xlsx."""
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # not a workbook
        raise ParseError(f"EIA-861 balancing authority file is not a readable workbook: {exc}") from None
    sheet = book.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = tuple(str(h).strip() if h is not None else "" for h in next(rows, ()))
    if header[: len(HEADER)] != HEADER:
        raise ParseError(f"EIA-861 balancing authority sheet: unexpected header {header!r}")
    out: dict[str, dict[str, Any]] = {}
    for values in rows:
        if not values or values[1] in (None, ""):
            continue
        year, ba_id, code, state, name = values[:5]
        code = str(code or "").strip()
        if not code:
            raise ParseError(f"EIA-861 balancing authority row without a BA code: {values!r}")
        entry = out.setdefault(code, {"year": int(year), "ba_ids": set(), "names": set(), "states": set()})
        entry["ba_ids"].add(int(ba_id))
        if name:
            entry["names"].add(str(name).strip())
        if state:
            entry["states"].add(str(state).strip())
    if not out:
        raise ParseError("EIA-861 balancing authority sheet has no rows")
    return {code: {"year": e["year"], "ba_ids": sorted(e["ba_ids"]), "names": sorted(e["names"]),
                   "states": sorted(e["states"])} for code, e in out.items()}


def parse_zip(data: bytes, year: int) -> dict[str, dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseError(f"EIA-861 {year} download is not a zip: {exc}") from None
    name = member_name(year)
    if name not in archive.namelist():
        raise ParseError(f"EIA-861 {year} zip has no {name}")
    return parse_xlsx(archive.read(name))


def fetch_latest(source_id: str, fetcher: Fetcher | None, today: date | None = None) -> tuple[DocumentRow, int]:
    today = today or date.today()
    last: FetchError | None = None
    for year in (today.year - 1, today.year - 2):
        try:
            doc = fetch_document(source_id, zip_url(year), doc_type="eia861_zip", title=f"EIA-861 {year}",
                                 external_id=f"f861{year}", fetcher=fetcher)
            return doc, year
        except FetchError as exc:
            if exc.status != 404:
                raise
            last = exc
    assert last is not None
    raise last


def region_for(code: str, entry: dict[str, Any], doc: DocumentRow) -> Region:
    meta = {**entry, "document_id": doc.id, "source_url": doc.url, "as_of": document_as_of(doc).isoformat(),
            "member": member_name(entry["year"])}
    name = entry["names"][0] if entry["names"] else code
    return Region(f"ba:{code}", "balancing_authority", name, "US", None, {"eia861": meta})


def load(doc: DocumentRow, year: int, stats: RunStats) -> dict[str, int]:
    table = parse_zip(doc.read(), year)
    out = UpsertStats()
    with db.transaction() as conn:
        grid_registry.ensure_registry(conn)
        for code, entry in sorted(table.items()):
            # EIA-930, the registry or HIFLD own the row's name, country and parent when they have it.
            out.add(upsert(conn, region_for(code, entry, doc), prefer_existing=True))
    stats.facts_seen += len(table)
    stats.facts_new += out.inserted + out.changed
    stats.extra.update(year=year, bas=len(table), rows=out.as_dict())
    return out.as_dict()


def run(*, source: SourceSpec = SOURCE, fetcher: Fetcher | None = None, today: date | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        doc, year = fetch_latest(source.id, fetcher, today)
        stats.document(doc)
        load(doc, year, stats)

    return collect(source, body)


@app.periodic(cron=CRON_UTC, periodic_id="monthly")
@app.task(name="collect.eia861_ba", queue="collect", queueing_lock="collect.eia861_ba", lock="collect.grid")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "eia861_ba", help="EIA-861 balancing authority ids and codes -> grid_region rows")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
