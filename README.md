# KOM Hunter

Descoberta pontual de segmentos Run perto de um ponto que ainda não corri
(ou corri sem KOM), ordenados por probabilidade de conseguir o KOM. Projeto
irmão do [club-koms](https://github.com/JustAnotherDud/club-koms) — só
reaproveita `comum.py`; propósito e cadência diferentes (isto é só para
mim, sob pedido, não um ranking diário multi-atleta).

## Fases

1. `explore.py` — tiles de segmentos (Mapbox Vector Tile) num raio à volta
   de um ponto, filtra Run, aplica um filtro GAP grosseiro (grade média +
   pace de referência fixo) para descartar candidatos claramente fora de
   alcance, escreve `candidatos.json` já ordenado e cortado a `--top`.
2. `segment_detail.py` — para cada candidato, página `/segments/<id>`:
   distância/grade/elevação exactas, leaderboard top 10 (KOM incluído),
   `athleteEffortCount` (já corri?), streams de elevação/distância. Cap
   explícito por corrida (`--max`) — um pedido por segmento é tráfego novo,
   não páginas que já visitarias normalmente.
3. Fase 3 (por escrever) — curva de pace/critical pace do Intervals.icu +
   modelo GAP completo (streams reais, não só grade média) para prever o
   meu tempo em cada segmento.
4. Fase 4 (por escrever) — ranking por `(previsto − KOM)`, output JSON.

## Uso

```bash
STRAVA_SESSION=<cookie _strava4_session> python explore.py \
    --lat 39.36 --lon -8.95 --raio 8 --pace-flat 3:40

STRAVA_SESSION=<cookie _strava4_session> python segment_detail.py \
    --in candidatos.json --out detalhes.json --so-nao-corridos
```

`STRAVA_SESSION` — cookie de sessão autenticada (DevTools → Application →
Cookies → strava.com → `_strava4_session`). Confirmado por teste: o
endpoint de tiles devolve 401 sem ele, mesmo com outro `athleteId` na URL —
não é só "security by obscurity".

`--pace-flat` é um pace de referência em plano (mm:ss/km), placeholder até
a Fase 3 trazer a curva de critical pace real — só serve para o pré-filtro
grosseiro da Fase 1, não para o ranking final.

## Env vars / secrets

- `STRAVA_SESSION` — cookie de sessão (renovar manualmente quando expirar)
- `INTERVALS_ICU_API_KEY` / `INTERVALS_ICU_ATHLETE_ID` — Fase 3 (auth
  directa via API key, não MCP — este repo corre standalone)

## Notas técnicas (do reconhecimento, 24 Jul 2026)

- `/maps/segments` (Explore) é uma SPA sem dados no HTML — os segmentos
  vêm de `cdn-1.strava.com/tiles/segments/<athleteId>/<z>/<x>/<y>` (Mapbox
  Vector Tile, `mapbox-vector-tile` descodifica). `activityType == 9` é Run
  (confirmado por spot-check).
- `/segments/<id>` é server-rendered — tudo vem no `__NEXT_DATA__`
  (`pageProps`), sem pedidos extra: `metadata`, `measurements`,
  `initialLeaderboard.leaderboard` (rank 1 = KOM), `athleteEffortCount`,
  `athletePrEffort.timing.elapsedTime`, `streams.{distance,elevation,location}`.
- `komElapsedTime` do tile bate certo com `leaderboard[0].elapsedTime` do
  detalhe (spot-check em 3 segmentos) — mesmo assim o detalhe é sempre a
  fonte de verdade; o tile só serve para o pré-filtro grosseiro.
