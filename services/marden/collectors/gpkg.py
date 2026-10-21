"""Read OGC GeoPackage files (SQLite) from bytes: feature tables, their SRS and each feature's WKB.

A GeoPackage geometry blob is a small header followed by standard WKB (OGC 12-128r19, section 2.1.3):

    'GP' | version (1 byte) | flags (1 byte) | srs_id (int32) | envelope (0, 32, 48 or 64 bytes) | WKB

flags bit 0 is the header byte order (1 = little endian), bits 1-3 the envelope size code and bit 4 marks an
empty geometry. PostGIS reads the WKB (ISO WKB with Z/M included) with ST_GeomFromWKB. The file is opened
in memory (`sqlite3.Connection.deserialize`), read only, so nothing in it is executed or written to disk.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


class GpkgError(ValueError):
    """The bytes are not a GeoPackage this reader understands."""


@dataclass(frozen=True)
class FeatureTable:
    name: str
    geometry_column: str
    geometry_type: str
    srs_id: int
    epsg: int | None  # the EPSG code of srs_id, when the file's gpkg_spatial_ref_sys says so


@dataclass(frozen=True)
class Feature:
    table: str
    fid: Any
    properties: dict[str, Any]
    wkb: bytes | None  # None for an empty or missing geometry
    srs_id: int


def wkb_from_blob(blob: bytes | None) -> tuple[bytes | None, int | None]:
    """(WKB, srs_id) from a GeoPackage geometry blob; (None, srs_id) for an empty geometry."""
    if blob is None:
        return None, None
    blob = bytes(blob)
    if len(blob) < 8 or blob[:2] != b"GP":
        raise GpkgError("geometry blob does not start with the 'GP' magic")
    flags = blob[3]
    endian = "<" if flags & 0x01 else ">"
    envelope_code = (flags >> 1) & 0x07
    if envelope_code not in ENVELOPE_BYTES:
        raise GpkgError(f"invalid envelope code {envelope_code} in geometry header")
    (srs_id,) = struct.unpack(f"{endian}i", blob[4:8])
    start = 8 + ENVELOPE_BYTES[envelope_code]
    if flags & 0x10 or len(blob) <= start:
        return None, srs_id
    return blob[start:], srs_id


def open_bytes(data: bytes) -> sqlite3.Connection:
    if not data.startswith(b"SQLite format 3\x00"):
        raise GpkgError("not an SQLite database (GeoPackage files are SQLite)")
    con = sqlite3.connect(":memory:")
    try:
        con.deserialize(data)
        con.execute("PRAGMA query_only = ON")
        con.execute("SELECT count(*) FROM gpkg_contents").fetchone()
    except sqlite3.DatabaseError as exc:
        con.close()
        raise GpkgError(f"not a GeoPackage: {exc}") from None
    return con


def feature_tables(con: sqlite3.Connection) -> list[FeatureTable]:
    rows = con.execute(
        """SELECT g.table_name, g.column_name, g.geometry_type_name, g.srs_id, s.organization,
                  s.organization_coordsys_id
           FROM gpkg_geometry_columns g
           JOIN gpkg_contents c ON c.table_name = g.table_name AND c.data_type = 'features'
           LEFT JOIN gpkg_spatial_ref_sys s ON s.srs_id = g.srs_id
           ORDER BY g.table_name"""
    ).fetchall()
    out = []
    for table, column, gtype, srs_id, org, org_id in rows:
        epsg = int(org_id) if org and str(org).upper() == "EPSG" and org_id is not None else None
        out.append(FeatureTable(table, column, gtype, int(srs_id), epsg))
    return out


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def iter_features(con: sqlite3.Connection, table: FeatureTable) -> Iterator[Feature]:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({_quote(table.name)})")]
    pk = next((r[1] for r in con.execute(f"PRAGMA table_info({_quote(table.name)})") if r[5]), None)
    cursor = con.execute(f"SELECT * FROM {_quote(table.name)}")
    for values in cursor:
        record = dict(zip(cols, values, strict=True))
        blob = record.pop(table.geometry_column, None)
        wkb, srs_id = wkb_from_blob(blob)
        fid = record.get(pk) if pk else None
        yield Feature(table.name, fid, record, wkb, srs_id if srs_id is not None else table.srs_id)


def read(data: bytes) -> tuple[list[FeatureTable], list[Feature]]:
    """All feature tables and their features (the files Marden reads have one table)."""
    con = open_bytes(data)
    try:
        tables = feature_tables(con)
        if not tables:
            raise GpkgError("the GeoPackage has no feature table")
        features = [f for t in tables for f in iter_features(con, t)]
    finally:
        con.close()
    return tables, features


def epsg_for(tables: list[FeatureTable], table_name: str) -> int:
    """The EPSG code to give PostGIS for a table's geometries (GeoPackage srs_id 4326/3857 are EPSG codes)."""
    for t in tables:
        if t.name == table_name:
            if t.epsg is not None:
                return t.epsg
            if t.srs_id in (4326, 3857):
                return t.srs_id
            raise GpkgError(f"table {table_name}: srs_id {t.srs_id} is not an EPSG code")
    raise GpkgError(f"no feature table {table_name!r}")
