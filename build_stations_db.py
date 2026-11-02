#!/usr/bin/env python3
from __future__ import annotations
"""
Build stations.sqlite (railway stations for train-leg anchoring) from GeoNames,
with local-language names from Trainline + GeoNames alternate names.

Usage:
    python3 build_stations_db.py                          # writes to Resources/
    python3 build_stations_db.py /path/to/out.sqlite
    python3 build_stations_db.py --csv stations.csv       # also export the merged set as CSV

Sources (all cached next to this script, gitignored; delete a file to refresh):
  1. GeoNames allCountries.zip (~370 MB, shared with build_harbours_db.py).
     The station list itself: RSTN (railroad station) + RSTP (railroad stop).
     MTRO (metro) is a separate code and never read; tram stops GeoNames files
     as RSTN ("Hlavní nádraží Tram Station") are dropped by name.
  2. GeoNames alternateNamesV2.zip (~205 MB) + countryInfo.txt — each station's
     name in its country's first official language.
  3. Trainline stations.csv (https://github.com/trainline-eu/stations, ODbL) —
     curated local-language European station names ("Praha hl.n.", "Brno hl.n.")
     and importance signals (main-station flag, rail operators serving it).
     Only rows with a UIC code are read: the rest are city metastations and
     bus stops (FlixBus / Distribusion).
     GeoNames misses many stations Trainline knows (Přerov, Olomouc hl.n. —
     Czechia has ~150 GeoNames stations against ~380 Trainline ones), so a
     Trainline station no GeoNames station claims is added on its own
     (feature code TRLN, id = −Trainline id, stable across rebuilds) unless it
     reads as something else: in France a stop without an SNCF station code
     (SNCF sells its coaches under UIC codes too), anywhere a name marking a bus
     stop, a Rhine boat pier or a border tariff point ("Aachen Süd (Gr)").

Display name, first hit wins:
  1. Trainline name of a UIC station within TRAINLINE_MATCH_KM (same country).
  2. GeoNames alternate name in the country's first language — Latin script only
     (a Czech user reads "Tokyo Station", not 東京駅).
  3. The GeoNames name (or its ASCII form when the name is not Latin script).
  Generic words ("Railway Station", "Station", "Železniční stanice", "-eki") are
  stripped from every tier — the app already shows a train icon.

Score (higher = the station a city stop should anchor at):
    RSTN 100 / RSTP 60
    + MAIN_NAME_BONUS when the name reads as a main station (Main, hl.n., Hbf, …)
    + TRAINLINE_MATCH_BONUS, + TRAINLINE_MAIN_BONUS for is_main_station
    + OPERATOR_BONUS per rail operator selling tickets to it (capped)
Rows are then proximity-deduped (~300 m, higher score wins): GeoNames carries
volunteer duplicates (Brno hl.n. is both "Brno Main Station" and
"Hlavní vlakové nádraží").

Licensing: the merged output contains Trainline (ODbL) names, so it is
published (with this script) in a public repo, crediting GeoNames (CC-BY 4.0)
and Trainline (ODbL).

GeoNames tab-separated columns are identical to build_cities_db.py.
"""

import argparse
import csv
import io
import math
import os
import re
import sqlite3
import unicodedata
import urllib.request
import zipfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "../Traviary/AppSources/Resources/stations.sqlite")

ALL_COUNTRIES_URL = "https://download.geonames.org/export/dump/allCountries.zip"
ALT_NAMES_URL     = "https://download.geonames.org/export/dump/alternateNamesV2.zip"
COUNTRY_INFO_URL  = "https://download.geonames.org/export/dump/countryInfo.txt"
TRAINLINE_URL     = "https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv"

ALL_COUNTRIES_ZIP = os.path.join(SCRIPT_DIR, "allCountries.zip")
ALT_NAMES_ZIP     = os.path.join(SCRIPT_DIR, "alternateNamesV2.zip")
COUNTRY_INFO_TXT  = os.path.join(SCRIPT_DIR, "countryInfo.txt")
TRAINLINE_CSV     = os.path.join(SCRIPT_DIR, "trainline-stations.csv")

# feature_code → base score
KEEP_CODES: dict[str, int] = {"RSTN": 100, "RSTP": 60}

# Tram stops filed as railway stations — dropped by name.
TRAM_PATTERN = re.compile(r"\b(tram|tramway|straßenbahn|tramvaj)\b", re.IGNORECASE)

# Names that read as a city's main station.
MAIN_NAME_PATTERN = re.compile(
    r"\b(main|central|centraal|centrale|centrum|hl\.\s?n\.?|hlavní|hlavna|hlavná|"
    r"główna|glowny|główny|hauptbahnhof|hbf|keleti|nyugati)\b",
    re.IGNORECASE,
)

# Generic words stripped from every name (Trainline's British names carry them too).
GENERIC_PATTERNS = [
    re.compile(r"^železniční stanice\s+", re.IGNORECASE),
    re.compile(r"\s*\b(railway station|railroad station|train station|rail station|"
               r"railway halt|station|railway)\b\s*", re.IGNORECASE),
    re.compile(r"[\s-]+eki$", re.IGNORECASE),   # romanized Japanese "station"
]

# Trainline-only stations: base score as an RSTN, and the code they're stored under.
TRAINLINE_ONLY_CODE  = "TRLN"
TRAINLINE_ONLY_SCORE = KEEP_CODES["RSTN"]

# Names Trainline gives non-rail stops (SNCF coaches, Köln-Düsseldorfer boats) and border
# tariff points — never added on their own.
NON_RAIL_PATTERN = re.compile(
    r"gare routi|halte routi|\bbus\b|\bzob\b|mairie|[ée]glise|h[oô]tel de ville|"
    r"office du tourisme|\bbourg\b|\bplace\b|\bKD\b|\(gr\)|fronti[eè]re",
    re.IGNORECASE,
)

# Trainline operator columns that sell *rail* tickets (bus/coach aggregators excluded).
RAIL_OPERATORS = ["sncf", "entur", "db", "cff", "obb", "trenitalia", "ntv",
                  "renfe", "cercanias", "atoc", "benerail", "westbahn"]

MAIN_NAME_BONUS       = 50
TRAINLINE_MATCH_BONUS = 20
TRAINLINE_MAIN_BONUS  = 40
OPERATOR_BONUS        = 10
OPERATOR_BONUS_CAP    = 30

TRAINLINE_MATCH_KM = 0.5    # a Trainline station this close is the same station
TRAINLINE_CELL_DEG = 0.01   # grid cell for the Trainline proximity index (~1 km)
DEDUPE_CELL_DEG    = 0.003  # ~300 m proximity dedupe grid (same as harbours)

SCHEMA = """
CREATE TABLE stations (
    geonameid     INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    country       TEXT    NOT NULL,
    latitude      REAL    NOT NULL,
    longitude     REAL    NOT NULL,
    feature_code  TEXT    NOT NULL,
    score         INTEGER DEFAULT 0
);
CREATE INDEX idx_stations_lat_lon ON stations(latitude, longitude);
"""

INSERT_SQL = "INSERT INTO stations VALUES (?,?,?,?,?,?,?)"


def download(url: str, cache_path: str) -> str:
    if os.path.exists(cache_path):
        size_mb = os.path.getsize(cache_path) / 1_048_576
        print(f"Reusing cached {os.path.basename(cache_path)} ({size_mb:.1f} MB)")
        return cache_path
    print(f"Downloading {url} …")
    tmp_path = cache_path + ".part"
    urllib.request.urlretrieve(url, tmp_path)
    os.replace(tmp_path, cache_path)
    print(f"  {os.path.getsize(cache_path) / 1_048_576:.1f} MB received")
    return cache_path


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 111.32 * math.hypot(x, lat2 - lat1)


def is_latin(text: str) -> bool:
    """True when every letter in `text` is Latin script."""
    return all("LATIN" in unicodedata.name(ch, "") for ch in text if ch.isalpha())


def strip_generic(name: str) -> str:
    cleaned = name
    for pattern in GENERIC_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–,")
    return cleaned or name


# ------------------------------------------------------------------ GeoNames

class Station:
    __slots__ = ("geonameid", "name", "asciiname", "country", "lat", "lon",
                 "fcode", "score", "source")

    def __init__(self, geonameid, name, asciiname, country, lat, lon, fcode, score):
        self.geonameid = geonameid
        self.name = name
        self.asciiname = asciiname
        self.country = country
        self.lat = lat
        self.lon = lon
        self.fcode = fcode
        self.score = score
        self.source = "geonames"


def geonames_stations(zip_path: str) -> list[Station]:
    stations: list[Station] = []
    trams = 0
    print("Scanning allCountries.txt …")
    with zipfile.ZipFile(zip_path) as zf, zf.open("allCountries.txt") as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) < 19:
                continue
            score = KEEP_CODES.get(f[7])
            if score is None:
                continue
            name, cc = f[1], f[8].strip().upper()
            if not name or len(cc) != 2:
                continue
            if TRAM_PATTERN.search(name):
                trams += 1
                continue
            try:
                stations.append(Station(int(f[0]), name, f[2] or None, cc,
                                        float(f[4]), float(f[5]), f[7], score))
            except ValueError:
                continue
    print(f"  GeoNames: {len(stations):,} stations ({trams:,} tram stops dropped)")
    return stations


def country_languages(path: str) -> dict[str, str]:
    """ISO alpha-2 → the country's first listed language ("cs", "de", "ru"…)."""
    languages: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) > 15 and fields[15]:
                languages[fields[0]] = fields[15].split(",")[0].split("-")[0]
    return languages


def local_alternate_names(zip_path: str, stations: list[Station],
                          languages: dict[str, str]) -> dict[int, str]:
    """geonameid → Latin-script name in the station country's first language.
    Preferred names win; colloquial and historic names are skipped."""
    wanted = {s.geonameid: languages.get(s.country) for s in stations}
    best: dict[int, tuple[bool, str]] = {}
    print("Scanning alternateNamesV2.txt …")
    with zipfile.ZipFile(zip_path) as zf, zf.open("alternateNamesV2.txt") as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) < 8:
                continue
            try:
                gid = int(f[1])
            except ValueError:
                continue
            lang = wanted.get(gid)
            if lang is None or f[2] != lang:
                continue
            if f[6] == "1" or f[7] == "1":          # colloquial / historic
                continue
            name = f[3].strip()
            if not name or not is_latin(name):
                continue
            preferred = f[4] == "1"
            current = best.get(gid)
            if current is None or (preferred and not current[0]):
                best[gid] = (preferred, name)
    print(f"  {len(best):,} stations have a Latin-script local-language name")
    return {gid: name for gid, (_, name) in best.items()}


# ----------------------------------------------------------------- Trainline

def trainline_index(path: str) -> dict[tuple[int, int], list[dict]]:
    index: dict[tuple[int, int], list[dict]] = {}
    count = 0
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if not row["uic"] or not row["latitude"] or row["is_city"] == "t":
                continue
            lat, lon = float(row["latitude"]), float(row["longitude"])
            operators = sum(row.get(f"{op}_is_enabled") == "t" for op in RAIL_OPERATORS)
            entry = {
                "id": int(row["id"]),
                "name": row["name"].strip(),
                "country": row["country"],
                "lat": lat,
                "lon": lon,
                "is_main": row["is_main_station"] == "t",
                "operators": operators,
                "has_tvs": bool(row["sncf_tvs_id"]),
            }
            key = (int(lat / TRAINLINE_CELL_DEG), int(lon / TRAINLINE_CELL_DEG))
            index.setdefault(key, []).append(entry)
            count += 1
    print(f"  Trainline: {count:,} UIC railway stations indexed")
    return index


def nearest_trainline(index, station: Station) -> tuple[dict, float] | None:
    cy, cx = int(station.lat / TRAINLINE_CELL_DEG), int(station.lon / TRAINLINE_CELL_DEG)
    best, best_km = None, TRAINLINE_MATCH_KM
    for gy in (cy - 1, cy, cy + 1):
        for gx in (cx - 1, cx, cx + 1):
            for entry in index.get((gy, gx), ()):
                # A coach stop or boat pier beside the station must not lend it its name.
                if entry["country"] != station.country or NON_RAIL_PATTERN.search(entry["name"]):
                    continue
                d = km_between(station.lat, station.lon, entry["lat"], entry["lon"])
                if d <= best_km:
                    best, best_km = entry, d
    return (best, best_km) if best else None


# --------------------------------------------------------------------- merge

def trainline_score(entry: dict) -> int:
    score = TRAINLINE_MATCH_BONUS
    if entry["is_main"]:
        score += TRAINLINE_MAIN_BONUS
    return score + min(OPERATOR_BONUS * entry["operators"], OPERATOR_BONUS_CAP)


def resolve_names_and_scores(stations, alt_names, tl_index) -> set[int]:
    """Names and scores every GeoNames station; returns the ids of the Trainline
    entries a GeoNames station claimed."""
    # Each Trainline station names only its closest GeoNames station: neighbouring
    # terminals (Moscow's Komsomolskaya Square has three within 500 m) are distinct
    # stations and keep their own names.
    claims: dict[int, tuple[float, int]] = {}   # id(trainline entry) → (km, geonameid)
    matches: dict[int, dict] = {}
    for s in stations:
        found = nearest_trainline(tl_index, s)
        if found:
            entry, d = found
            matches[s.geonameid] = entry
            if id(entry) not in claims or d < claims[id(entry)][0]:
                claims[id(entry)] = (d, s.geonameid)
    for s in stations:
        match = matches.get(s.geonameid)
        if match and claims[id(match)][1] != s.geonameid:
            match = None
        if match:
            s.name = strip_generic(match["name"])
            s.source = "trainline"
            s.score += trainline_score(match)
        else:
            local = alt_names.get(s.geonameid)
            if local:
                base, s.source = local, "local"
            elif is_latin(s.name):
                base = s.name
            else:
                base = s.asciiname or s.name
            s.name = strip_generic(base)
        if MAIN_NAME_PATTERN.search(s.name):
            s.score += MAIN_NAME_BONUS
    return {entry_id for entry_id in claims}


def trainline_only_stations(tl_index, claimed: set[int]) -> list[Station]:
    """Trainline stations no GeoNames station claimed, kept unless they read as non-rail.
    No rail-operator test: Trainline sells no PKP tickets, yet Kutno is a station."""
    added: list[Station] = []
    skipped = 0
    for entries in tl_index.values():
        for entry in entries:
            if id(entry) in claimed:
                continue
            name = entry["name"]
            # SNCF also sells its coaches under UIC codes; only a station code marks a French halt.
            if ((entry["country"] == "FR" and not entry["has_tvs"]) or NON_RAIL_PATTERN.search(name) or TRAM_PATTERN.search(name)
                    or not is_latin(name)):
                skipped += 1
                continue
            s = Station(-entry["id"], name, None, entry["country"], entry["lat"], entry["lon"],
                        TRAINLINE_ONLY_CODE, TRAINLINE_ONLY_SCORE + trainline_score(entry))
            s.name = strip_generic(name)
            s.source = "trainline"
            if MAIN_NAME_PATTERN.search(s.name):
                s.score += MAIN_NAME_BONUS
            added.append(s)
    print(f"  Trainline only: {len(added):,} stations added ({skipped:,} non-rail skipped)")
    return added


def dedupe(stations: list[Station]) -> list[Station]:
    """Highest score first; a station lands only if no accepted one sits within
    one ~300 m grid neighbourhood. Ties prefer a Trainline / local name."""
    source_rank = {"trainline": 2, "local": 1, "geonames": 0}
    ordered = sorted(stations, key=lambda s: (s.score, source_rank[s.source]), reverse=True)
    taken: dict[tuple[int, int], list[Station]] = {}
    result: list[Station] = []
    for s in ordered:
        cx, cy = int(s.lon / DEDUPE_CELL_DEG), int(s.lat / DEDUPE_CELL_DEG)
        clash = any(
            abs(o.lat - s.lat) < DEDUPE_CELL_DEG and abs(o.lon - s.lon) < DEDUPE_CELL_DEG
            for gx in (cx - 1, cx, cx + 1)
            for gy in (cy - 1, cy, cy + 1)
            for o in taken.get((gx, gy), ())
        )
        if clash:
            continue
        taken.setdefault((cx, cy), []).append(s)
        result.append(s)
    print(f"Dedupe: {len(stations):,} → {len(result):,} stations")
    return result


def build_db(stations: list[Station], out_path: str) -> None:
    if os.path.exists(out_path):
        os.remove(out_path)
    conn = sqlite3.connect(out_path)
    cur = conn.cursor()
    cur.executescript(SCHEMA)
    cur.executemany(INSERT_SQL, [
        (s.geonameid, s.name, s.country, s.lat, s.lon, s.fcode, s.score) for s in stations
    ])
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    size_mb = os.path.getsize(out_path) / 1_048_576
    print(f"Database: {len(stations):,} stations, {size_mb:.1f} MB  →  {out_path}")


def write_csv(stations: list[Station], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["geonameid", "name", "country", "latitude", "longitude",
                         "feature_code", "score", "name_source"])
        for s in sorted(stations, key=lambda s: (s.country, s.name)):
            writer.writerow([s.geonameid, s.name, s.country, f"{s.lat:.6f}", f"{s.lon:.6f}",
                             s.fcode, s.score, s.source])
    print(f"CSV: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build stations.sqlite from GeoNames + Trainline.")
    parser.add_argument("out", nargs="?", default=DEFAULT_OUT)
    parser.add_argument("--csv", help="also write the merged station set as CSV")
    args = parser.parse_args()
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    stations = geonames_stations(download(ALL_COUNTRIES_URL, ALL_COUNTRIES_ZIP))
    languages = country_languages(download(COUNTRY_INFO_URL, COUNTRY_INFO_TXT))
    alt_names = local_alternate_names(download(ALT_NAMES_URL, ALT_NAMES_ZIP), stations, languages)
    tl_index = trainline_index(download(TRAINLINE_URL, TRAINLINE_CSV))

    claimed = resolve_names_and_scores(stations, alt_names, tl_index)
    stations = dedupe(stations + trainline_only_stations(tl_index, claimed))
    sources: dict[str, int] = {}
    for s in stations:
        sources[s.source] = sources.get(s.source, 0) + 1
    print("Name sources: " + ", ".join(f"{k} {v:,}" for k, v in sorted(sources.items())))

    build_db(stations, out_path)
    if args.csv:
        write_csv(stations, os.path.abspath(args.csv))
    print("Done.")


if __name__ == "__main__":
    main()
