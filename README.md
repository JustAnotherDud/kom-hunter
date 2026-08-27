# KOM Hunter

On-demand discovery of Strava **Run** segments near a point I haven't run yet
(or ran without taking the KOM), ranked by how likely I am to get the KOM.
Sister project to
[folha-do-clube](https://github.com/JustAnotherDud/folha-do-clube) — it only
reuses `comum.py`; different purpose and cadence (this one is just for me, on
request, not a daily multi-athlete ranking).

It reads Strava segment tiles through my own authenticated session cookie, at
human cadence, with an explicit per-run request cap. That makes it a personal
tool rather than something to point at anyone else's data — see **Auth** below.

## Phases

1. `explore.py` — segment tiles (Mapbox Vector Tile) in a radius around a
   point, filters Run, computes a rough GAP (mean grade + fixed reference
   pace) only to **order** the queue (most promising first) — never to
   exclude. Tested (25 Jul 2026): an earlier version that excluded by margin
   dropped candidates the good model later placed ~2s off the KOM. Writes
   `candidatos.json` already sorted, trimmed only by `--top`. Segments with no
   `komElapsedTime` in the tile (rare, missing data in Strava's backend —
   confirmed, not "nobody tried") go separately to `sem_kom.json` for manual
   inspection via link.
2. `segment_detail.py` — for each candidate, the `/segments/<id>` page: exact
   distance/grade/elevation, top-10 leaderboard (KOM included),
   `athleteEffortCount` (have I run it?), elevation/distance streams. Explicit
   per-run cap (`--max`) — one request per segment is new traffic, not pages
   you would visit anyway.
3. `gap_model.py` — prediction of my time per segment, from the Intervals.icu
   GAP pace curve (direct API key) + effective distance computed
   point-by-point over the streams. Two distinct confidence levels in the
   output (see "Four groups" below) — it does not fake precision the model
   does not have for short segments.
4. `rank.py` — final orchestrator with a persistent per-segment
   `historico.json`. It only redoes Phase 2+3 for a segment if: it is not in
   the history yet, the KOM changed (compared for free — it already comes in
   Phase 1's `candidatos.json`, no extra request), or `--revisao-semanas`
   have passed since the last analysis (default 4 — my own capability moves,
   not just other people's KOMs). Otherwise it reuses the stored score, zero
   new requests. Output `ranking.json` in the same groups as `gap_model.py`,
   each sorted by `score` (predicted/heuristic − KOM) ascending — most
   achievable first.

## Usage

Normal path (2 commands — `rank.py` already calls Phase 2 internally per
segment, no need to run `segment_detail.py` separately):

```bash
STRAVA_SESSION=<_strava4_session cookie> python explore.py \
    --lat <LAT> --lon <LON> --athlete-id <ID> --pace-flat 3:40

STRAVA_SESSION=<_strava4_session cookie> python rank.py \
    --in candidatos.json --out ranking.json --pace-flat 3:40
```

`segment_detail.py` and `gap_model.py` are still useful run separately just to
inspect the raw `detalhes.json` / `previsoes.json` of a batch without touching
the persistent history — they are not part of the normal path.

`--pace-flat` (explore.py and gap_model.py/rank.py) is a flat reference pace
(mm:ss/km) used only in the rough heuristics — never in the high-confidence
model (that one comes from the real GAP curve, no manual input). Use a genuine
effort pace (short, chasing a KOM), not your normal training pace — only
validated so far against `3:40/km`.

## Auth

`STRAVA_SESSION` — an authenticated session cookie (DevTools → Application →
Cookies → strava.com → `_strava4_session`). Confirmed by test: the tiles
endpoint returns 401 without it, and also 401 if the `--athlete-id` in the URL
is not the session's own athlete — it is not just security by obscurity.
Renew it manually when it expires.

This is why the tool is personal: it reads Strava through my own logged-in
session, on my own request, at human cadence (explicit `--max` cap per run).
It is not a scraper pointed at other people's data and it does not run
unattended.

## Zoom and radius (explore.py) — measured, not guessed

Real segment density only stabilises from **z15** (~19/km² in the area I
tested, 25 Jul 2026); at z10 (the old default) it measured **~170× lower** —
not a shortage of segments in the area, just normal pyramidal-tile decimation
at low zoom (the map shows less detail when zoomed out, like any vector map).
So:

- default `--zoom 15`, default `--raio 1.5` (km) — area grows with the square
  of the radius, so fine zoom is only sustainable at small radii within a
  reasonable request count (`MAX_TILES=40`, covers up to ~2.5 km radius).
- **For areas larger than ~2 km: run several searches with different centres,
  do not raise `--raio`.** Raising the radius at zoom 15 grows the tile count
  quadratically (3 km radius already exceeds 40 tiles); lowering the zoom to
  fit a bigger radius undersamples again. No automation for this yet (new
  scope, undecided if it is worth it).
- `explore.py` always prints the real coverage (tiles requested, km² covered
  vs requested, observed density) — so you never have to guess whether a low
  candidate count is "segment-poor area" or "incomplete search".
- `intent=explore` in the tile request, never `intent=popular` — tested:
  `popular` returns fewer than 2/3 of what any other value (`explore`,
  `browse`, `nearby`, `top`, `recent` all gave the same larger result)
  returns for the same tile/zoom.

## Four groups in the output (gap_model.py and rank.py)

Validation (24-25 Jul 2026) showed the Intervals.icu GAP pace curve is only
reliable above ~1000 m effective distance, and that the short heuristic
overvalues descents — see "Technical notes". So the output has 4 groups, not
2:

- **`confianca_alta`** (effective ≥ 1000 m) — `previsto_s` comes from the real
  GAP model (curve table or critical-speed CS/D'), uncorrected. Tested against
  5 real segments: error of -15% to -35%, always in the same direction — an
  explicit decision not to correct it, because the model measures the
  capability ceiling (best effort), not casual training pace, and that is the
  question the tool exists to answer ("if I go for it, can I beat the KOM?").
- **`confianca_especulativa_plano_subida`** (effective < 1000 m, mean grade
  ≥ 0) — no real coverage in the GAP curve (endurance training does not
  produce structured sprint data). `heuristica_s` uses, in order: real curve
  points that survive the speed filter in that range (rare), else effective
  grade × `--pace-flat`, else Phase 1's `previsto_grosseiro_s`. Tested against
  the only 2 short segments with a known real time (94 s, 64 s — where I
  already hold the KOM): error of -4% to -8% with `--pace-flat 3:40` — but it
  is heuristic, not physics, and only tested with this pace; do not generalise
  without a new test.
- **`confianca_especulativa_descida_SEM_CONFIANCA`** (effective < 1000 m, mean
  grade < 0) — **not sorted, does not enter the decision of where to hunt.**
  The effective distance reduced by Minetti on a descent, combined with a
  single `--pace-flat` (meant for flat, not descent speed), visibly
  overvalued these segments in testing: one never-run segment came out "more
  beatable" than two where I already hold the KOM. Zero real descent effort in
  the history to calibrate — kept aside until there is.
- **`revisao_manual`** — any never-run segment whose prediction beats the KOM
  by more than `MARGEM_SUSPEITA_PCT` (15%, `gap_model.py`) drops out of the
  normal ranking into here. Automatic sanity guard-rail, not descent-specific
  — beating someone else's KOM by a large margin on an unknown segment is more
  likely a model error than sudden talent.

## Env vars / secrets

- `STRAVA_SESSION` — session cookie (renew manually when it expires)
- `STRAVA_ATHLETE_ID` — numeric Strava athlete id for the session above
  (or pass `--athlete-id`); required, no default
- `INTERVALS_ICU_API_KEY` / `INTERVALS_ICU_ATHLETE_ID` — direct Basic Auth
  (literal username `API_KEY`, password is the key — confirmed on the official
  Intervals.icu forum). `id=0` in the path works as "the key's athlete". Keep
  it in a local `.env` (already in `.gitignore`) — `carregar_env()` in
  `gap_model.py` reads it with no extra dependencies.

## Technical notes (from the recon, 24 Jul 2026)

- `/maps/segments` (Explore) is an SPA with no data in the HTML — segments
  come from `cdn-1.strava.com/tiles/segments/<athleteId>/<z>/<x>/<y>` (Mapbox
  Vector Tile, decoded by `mapbox-vector-tile`). `activityType == 9` is Run
  (confirmed by spot-check).
- `/segments/<id>` is server-rendered — everything comes in `__NEXT_DATA__`
  (`pageProps`), no extra requests: `metadata`, `measurements`,
  `initialLeaderboard.leaderboard` (rank 1 = KOM), `athleteEffortCount`,
  `athletePrEffort.timing.elapsedTime`,
  `streams.{distance,elevation,location}`.
- `komElapsedTime` from the tile matches `leaderboard[0].elapsedTime` from the
  detail (spot-check on 3 segments) — even so the detail is always the source
  of truth; the tile only serves the rough pre-filter.
- `GET /api/v1/athlete/{id}/pace-curves.json?gap=true` from Intervals.icu
  returns a `distance[]` / `values[]` curve (time in s) + `paceModels`
  (critical speed `CS` / `dPrime`), already GAP-normalised. Finding: between
  ~45 m and ~900 m, the table came entirely from ONE run (10K, 8 Mar 2026)
  with GPS noise at the start — implying 41-55 km/h, impossible.
  `custo_minetti()` in `comum.py` now clamps grade to ±45% (Minetti's
  5th-degree polynomial blows up outside that range) and
  `distancia_efetiva_streams()` in `gap_model.py` resamples the streams at
  steps ≥5 m before computing grade (Strava streams come with irregular
  spacing, sometimes <0.5 m — at that scale normal GPS/altimeter noise already
  implies grades of hundreds of %). `gap_model.py` also filters GAP curve
  points with implied speed > 24 km/h before using them — permanent
  guard-rail, not just for this case.
