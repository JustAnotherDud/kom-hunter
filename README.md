# KOM Hunter

Finds Strava Run segments near a point where a KOM looks reachable, ranked by
predicted margin. Sister project of
[folha-do-clube](https://github.com/JustAnotherDud/folha-do-clube).

It is a personal tool. It reads Strava through my own session cookie, only
when I run it, with a cap on requests per run. Do not point it at anyone
else's data.

## Usage

```bash
STRAVA_SESSION=<cookie> python explore.py \
    --lat <LAT> --lon <LON> --athlete-id <ID> --pace-flat 3:40

STRAVA_SESSION=<cookie> python rank.py \
    --in candidatos.json --out ranking.json --pace-flat 3:40
```

`rank.py` runs phases 2 and 3 itself. Run `segment_detail.py` or
`gap_model.py` alone only to inspect `detalhes.json` or `previsoes.json`
without touching the history.

`--pace-flat` is a flat reference pace (mm:ss/km) used only by the rough
heuristics. Use a hard effort pace, not training pace. Only tested with 3:40.

## Phases

1. `explore.py` reads segment tiles around a point and keeps Run segments. A
   rough estimate (mean grade and `--pace-flat`) sorts the queue but never
   excludes: an older version that excluded dropped segments the real model
   placed ~2 s off the KOM. Writes `candidatos.json`, cut only by `--top`.
   Segments with no `komElapsedTime` in the tile go to `sem_kom.json`. That is
   missing data on Strava's side, not an empty leaderboard.
2. `segment_detail.py` fetches `/segments/<id>` for each candidate: exact
   measurements, top 10, `athleteEffortCount` and distance/elevation streams.
   `--max` caps requests per run.
3. `gap_model.py` predicts my time from the Intervals.icu GAP pace curve and
   an effective distance computed over the streams.
4. `rank.py` runs phases 2 and 3 per segment and keeps `historico.json`. It
   only redoes a segment if it is new, its KOM changed (the tile already has
   it, so the check is free) or `--revisao-semanas` passed (default 4).
   `--max-novos` caps redone segments per run.

## Output groups

`gap_model.py` and `rank.py` write five groups. `score` (rank) and
`gap_para_kom_s` (gap_model) are prediction minus KOM in seconds. Lower is
better.

- `confianca_alta`: effective distance of 1000 m or more. Prediction from the
  curve table, or the CS/D' model past its end. Sorted. On 5 real segments it
  came out 15-35% faster than my PRs, always. Not corrected on purpose: the
  curve measures my ceiling, and that is the question here.
- `confianca_especulativa_plano_subida`: shorter, mean grade 0 or more. The
  curve has no real sprint data, so a heuristic is used, in this order: curve
  points that pass the speed filter, effective distance times `--pace-flat`,
  the phase 1 estimate. Sorted. On 2 real segments: 4-8% error with 3:40.
- `confianca_especulativa_descida_SEM_CONFIANCA`: shorter, mean grade below 0.
  No descent data to calibrate. Not sorted. Do not use it to pick targets.
- `fora_alcance_curva_SEM_CONFIANCA`: the CS/D' model used past 1.5 times the
  longest curve point (ultras). Not sorted.
- `revisao_manual`: a never-run segment whose prediction beats the KOM by more
  than 15% (`MARGEM_SUSPEITA_PCT`). More likely a model error than talent.

## Env vars

- `STRAVA_SESSION`: the `_strava4_session` cookie (DevTools, Application,
  Cookies, strava.com). Renew it by hand when it expires.
- `STRAVA_ATHLETE_ID` or `--athlete-id`: your Strava athlete id, required by
  `explore.py`. The tile endpoint returns 401 if it does not match the session.
- `INTERVALS_ICU_API_KEY` and `INTERVALS_ICU_ATHLETE_ID`: Basic Auth with
  username `API_KEY`. Athlete id `0` means the key's own athlete. They can live
  in a local `.env` (gitignored).

## Search area (explore.py)

- Defaults: `--zoom 15`, `--raio 1.5` km. Lower zooms drop segments (z10 had
  ~170 times fewer).
- `MAX_TILES=40` caps tile requests, about a 2.5 km radius at z15. For larger
  areas, run several centres instead of raising `--raio`.
- The script prints coverage and observed density (~19/km² in my test area),
  so you can tell a poor area from an incomplete search.
- It uses `intent=explore`. `intent=popular` returns at least a third fewer
  segments.

## Technical notes

- Segment tiles: `cdn-1.strava.com/tiles/segments/<athleteId>/<z>/<x>/<y>`
  (Mapbox Vector Tile). `activityType == 9` is Run.
- `/segments/<id>` is server-rendered. All data is in `__NEXT_DATA__`
  (`pageProps`).
- The tile's `komElapsedTime` matched the detail page in spot checks. The
  detail page is the source of truth.
- Intervals.icu: `GET /api/v1/athlete/{id}/pace-curves.json?gap=true` returns
  `distance[]` and `values[]` plus `paceModels` (CS and `dPrime`).
- Guard rails against GPS noise: curve points faster than 24 km/h are dropped,
  streams are resampled to steps of 5 m or more before computing grade, and
  `custo_minetti()` clamps grade to ±45%.
