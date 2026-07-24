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
3. `gap_model.py` — previsão do meu tempo por segmento, a partir da curva
   de pace GAP do Intervals.icu (API key directa) + distância efectiva
   calculada ponto-a-ponto sobre os streams. Duas confianças distintas no
   output (ver "Duas confianças" abaixo) — não finge precisão que o
   modelo não tem para segmentos curtos.
4. Fase 4 (por escrever) — persistência (histórico de score/KOM por
   segmento, recalcular só quando o KOM muda ou de X em X semanas) +
   ranking final, output JSON.

## Uso

```bash
STRAVA_SESSION=<cookie _strava4_session> python explore.py \
    --lat 39.36 --lon -8.95 --raio 8 --pace-flat 3:40

STRAVA_SESSION=<cookie _strava4_session> python segment_detail.py \
    --in candidatos.json --out detalhes.json --so-nao-corridos

python gap_model.py --in detalhes.json --out previsoes.json --pace-flat 3:40
```

`STRAVA_SESSION` — cookie de sessão autenticada (DevTools → Application →
Cookies → strava.com → `_strava4_session`). Confirmado por teste: o
endpoint de tiles devolve 401 sem ele, mesmo com outro `athleteId` na URL —
não é só "security by obscurity".

`--pace-flat` (explore.py e gap_model.py) é um pace de referência em plano
(mm:ss/km) usado só nas heurísticas grosseiras — nunca no modelo de
confiança alta (esse vem da curva GAP real, sem input manual).

## Duas confianças (gap_model.py)

Validação (24 Jul 2026) mostrou que a curva de pace GAP do Intervals.icu só
é fiável acima de ~1000m de distância efectiva — ver "Notas técnicas".
Por isso o output separa:

- **`confianca: "alta"`** (efectiva ≥ 1000m) — `previsto_s` vem do modelo
  GAP real (tabela da curva ou critical-speed CS/D'), sem correcção. Testado
  contra 5 segmentos reais: erro de -15% a -35%, sempre na mesma direcção —
  decisão explícita de não corrigir, porque o modelo mede o tecto de
  capacidade (melhor esforço), não o ritmo casual de treino, e essa é a
  pergunta da ferramenta ("se for a sério, consigo bater o KOM?").
- **`confianca: "especulativa"`** (efectiva < 1000m) — sem cobertura real
  na curva GAP (treino de fundo não gera dados de sprint estruturado).
  `heuristica_s` usa, por ordem: pontos reais da curva que sobrevivam ao
  filtro de velocidade nessa gama (raro), senão grade efectiva ×
  `--pace-flat`, senão o `previsto_grosseiro_s` já vindo da Fase 1.
  `heuristica_metodo` diz sempre qual foi usado. Testado contra os únicos 2
  segmentos curtos com tempo real conhecido (94s, 64s — onde já sou o KOM):
  erro de -4% a -8% com `--pace-flat 3:40` — mas é heurística, não física;
  não generalizar a precisão para outros paces/segmentos sem novo teste.

## Env vars / secrets

- `STRAVA_SESSION` — cookie de sessão (renovar manualmente quando expirar)
- `INTERVALS_ICU_API_KEY` / `INTERVALS_ICU_ATHLETE_ID` — Basic Auth directa
  (username literal `API_KEY`, password a chave — confirmado no fórum
  oficial do Intervals.icu). `id=0` no path funciona como "atleta da
  chave". Guarda num `.env` local (já no `.gitignore`) — `carregar_env()`
  em `gap_model.py` lê-o sem dependências extra.

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
- `GET /api/v1/athlete/{id}/pace-curves.json?gap=true` do Intervals.icu
  devolve uma curva `distance[]`/`values[]` (tempo em s) + `paceModels`
  (critical speed `CS`/`dPrime`) já GAP-normalizada. Achado: entre ~45m e
  ~900m, a tabela vinha inteira de UMA corrida (10K, 8 Mar 2026) com ruído
  de GPS no arranque — implicava 41-55 km/h, impossível. `custo_minetti()`
  em `comum.py` agora limita a grade a ±45% (o polinómio de Minetti de 5º
  grau explode fora desse intervalo) e `distancia_efetiva_streams()` em
  `gap_model.py` reamostra os streams a passos ≥5m antes de calcular grade
  (streams da Strava vêm com espaçamento irregular, por vezes <0.5m —
  nessa escala o ruído normal de GPS/altímetro já implica grades de
  centenas de %). `gap_model.py` ainda filtra pontos da curva GAP com
  velocidade implícita > 24km/h antes de os usar — guarda-rail permanente,
  não só para este caso.
