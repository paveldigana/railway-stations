# traviary-stations

About 75,000 railway stations in 141 countries, named in each country's own language where it uses the Latin alphabet. Built from [GeoNames](https://www.geonames.org) and [Trainline's open station list](https://github.com/trainline-eu/stations).

The [Traviary](https://traviary.online) iOS travel journal uses it, fully offline, to anchor train legs at real stations: a trip to Prague starts and ends at **Praha hl.n.**, not at "Prague Main Railway Station" or a suburban halt.

It's meant for maps and travel journals, **not navigation**. Coverage is uneven: dense in Europe, Japan and India, thin in some countries.

## Files

| File | What it is |
|---|---|
| `stations.csv` | The merged station set (UTF-8, comma-separated) |
| `build_stations_db.py` | The script that builds it (Python 3, standard library only) |
| `LICENSE` | ODC Open Database License (ODbL) 1.0 |

### `stations.csv` columns

| Column | Meaning |
|---|---|
| `geonameid` | GeoNames id; links back to `https://www.geonames.org/<id>` |
| `name` | Display name (see *Names* below) |
| `country` | ISO 3166-1 alpha-2 |
| `latitude`, `longitude` | WGS-84 |
| `feature_code` | GeoNames `RSTN` (railroad station) or `RSTP` (railroad stop) |
| `score` | Importance. Higher means more likely the station a city's trips should anchor at |
| `name_source` | Where the name came from: `trainline`, `local` (GeoNames alternate name) or `geonames` |

## How it's built

1. **Stations:** GeoNames `RSTN` + `RSTP`. Metro has its own code and isn't included. Tram stops GeoNames files as stations are dropped by name.
2. **Names**, first match wins:
   1. **Trainline:** if a Trainline station with a UIC code is within 500 m, its curated local name ("Praha hl.n.", "Brno hl.n.", "Mainz Hbf"). Each Trainline station names only its closest GeoNames station, so neighbouring terminals keep their own names.
   2. **GeoNames alternate name** in the country's first official language, **Latin script only**. Japan, Russia, China, etc. keep the Latin GeoNames name ("Tōkyō", not 東京駅).
   3. **The GeoNames name.**

   Generic words ("Railway Station", "Station", "Železniční stanice", "-eki") are removed.
3. **Score:** stations score above stops. Bonuses go to main-station names (Main, Central, hl.n., Hbf, …), Trainline's main-station flag, and the number of rail operators selling tickets to the station.
4. **Duplicates:** stations within ~300 m are merged, keeping the higher score.

To rebuild (downloads about 600 MB of source data on the first run and caches it next to the script):

```bash
python3 build_stations_db.py stations.sqlite --csv stations.csv
```

## Licence and attribution

The database is licensed under the **[Open Database License (ODbL) 1.0](LICENSE)**. If you use it, credit:

- **GeoNames** (<https://www.geonames.org>), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- **Trainline stations** (<https://github.com/trainline-eu/stations>), licensed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)

If you publicly use a modified version of this database, ODbL requires you to share that version under the same licence.
