# -*- coding: utf-8 -*-
"""explore.py — Fase 1: descoberta de segmentos Run candidatos perto de um
ponto, com pré-filtro GAP grosseiro (usa só os campos que já vêm no tile —
distância, grade média, komElapsedTime — sem tocar em /segments/<id>).

    STRAVA_SESSION=<cookie> python explore.py --lat 39.36 --lon -8.95 \
        --raio 1.5 --pace-flat 3:40

--pace-flat é um pace de referência em plano (mm:ss/km) — placeholder até a
Fase 3 trazer a curva de critical pace real do Intervals.icu (o pace
sustentável varia com a duração do esforço; isto assume-o fixo, por isso só
serve para descartar candidatos claramente fora de alcance, não para o
ranking final).

Zoom/raio (25 Jul 2026 — medido, não escolhido às cegas): a densidade real
de segmentos só estabiliza a partir de z15 (~19/km² em Rio Maior); a z10
media-se ~170x menos — não falta de segmentos na zona, é decimação normal
de tile piramidal a zoom baixo. Por isso o default é zoom=15, o que por sua
vez limita o raio prático a ~1.5-2km dentro de um número razoável de
pedidos (a área cresce com o quadrado do raio). PARA ÁREAS MAIORES: corre
várias buscas com centros diferentes, não subas o raio — ver README.

`intent=explore` no pedido ao tile (não "popular" — testado: "popular"
filtra a menos de 2/3 do que qualquer outro valor devolve no mesmo tile).

Escreve candidatos.json, ordenado por gap_grosseiro_s (mais exequíveis
primeiro) e já cortado a --top — é isso que o segment_detail.py (Fase 2)
deve consumir, para não gastar um pedido por segmento em toda a zona.
"""
import argparse
import json
import math
import os
import sys
import time

from comum import (ACTIVITY_TYPE_RUN, INTENT_DEFAULT, PAGE_DELAY, largura_tile_km,
                    obter_tile_segmentos, sessao_strava, tempo_previsto_grosseiro,
                    tiles_no_raio)

MAX_TILES = 40  # cap de pedidos de tiles por corrida — cobre até ~raio 2.5km a zoom 15
ATHLETE_ID_DEFAULT = "100300630"  # José — já público via club-koms


def parse_pace(s):
    """'3:40' -> segundos por km (int)."""
    m, sec = s.split(":")
    return int(m) * 60 + int(sec)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--raio", type=float, default=1.5,
                     help="raio em km (default 1.5 — ver docstring: zoom=15 não escala "
                          "bem para raios grandes, corre várias buscas em vez de subir isto)")
    ap.add_argument("--zoom", type=int, default=15,
                     help="zoom das tiles (default 15 — medido: densidade real só a "
                          "partir daqui, ver docstring)")
    ap.add_argument("--intent", default=INTENT_DEFAULT,
                     help=f"parâmetro intent do tile (default '{INTENT_DEFAULT}' — "
                          "NUNCA 'popular', filtra muito)")
    ap.add_argument("--pace-flat", required=True, dest="pace_flat",
                     help="pace de referência em plano, mm:ss/km")
    ap.add_argument("--margem", type=float, default=1.15,
                     help="descarta candidatos com previsão > KOM * margem (default 1.15)")
    ap.add_argument("--athlete-id",
                     default=os.environ.get("STRAVA_ATHLETE_ID", ATHLETE_ID_DEFAULT))
    ap.add_argument("--top", type=int, default=40,
                     help="máx. de candidatos no output (default 40 — é o que a Fase 2 vai processar)")
    ap.add_argument("--out", default="candidatos.json")
    args = ap.parse_args()

    cookie = os.environ.get("STRAVA_SESSION", "").strip()
    if not cookie:
        sys.exit("STRAVA_SESSION não definido.")

    pace_flat = parse_pace(args.pace_flat)
    s = sessao_strava(cookie)

    tiles = tiles_no_raio(args.lat, args.lon, args.raio, args.zoom)
    if len(tiles) > MAX_TILES:
        sys.exit(f"{len(tiles)} tiles (> {MAX_TILES}) para este raio/zoom — "
                  "reduz --raio (não subas --zoom para compensar) ou corre em vários centros.")

    largura = largura_tile_km(args.lat, args.zoom)
    area_coberta = len(tiles) * largura ** 2
    area_pedida = math.pi * args.raio ** 2
    print(f"cobertura: {len(tiles)} tiles z{args.zoom} (~{largura:.2f}km/lado) = "
          f"~{area_coberta:.1f}km² cobertos vs ~{area_pedida:.1f}km² pedidos "
          f"(raio {args.raio}km) — intent={args.intent}")

    total_bruto = 0
    vistos = {}
    for i, (z, x, y) in enumerate(tiles):
        feats = obter_tile_segmentos(s, args.athlete_id, z, x, y, intent=args.intent)
        total_bruto += len(feats)
        for f in feats:
            p = f["properties"]
            if p.get("activityType") != ACTIVITY_TYPE_RUN:
                continue
            vistos[p["segmentId"]] = p  # dedupe — tiles adjacentes sobrepõem-se
        print(f"  tile {i + 1}/{len(tiles)} (z{z}/{x}/{y}): "
              f"{len(feats)} segmentos, {len(vistos)} Run acumulados")
        if i < len(tiles) - 1:
            time.sleep(PAGE_DELAY)

    densidade = total_bruto / area_coberta if area_coberta else 0
    print(f"densidade observada: {total_bruto} segmentos (todos os tipos) / "
          f"~{area_coberta:.1f}km² cobertos = ~{densidade:.1f}/km² "
          "(referência Rio Maior z15 c/ intent≠popular: ~19/km² — se sair muito abaixo "
          "disto noutra zona, pode ser zona pobre em segmentos OU busca incompleta; "
          "sobe --zoom ou baixa --raio para confirmar)")

    candidatos = []
    sem_kom = 0
    for sid, p in vistos.items():
        kom = p.get("komElapsedTime")
        if not kom:
            # a zoom fino (z15+) aparecem segmentos raramente/nunca corridos —
            # sem esforços não há KOM a bater, não são candidatos válidos aqui
            sem_kom += 1
            continue
        previsto = tempo_previsto_grosseiro(p["distance"], p["avgGrade"], pace_flat)
        candidatos.append({
            "segmentId": sid,
            "nome": p["name"],
            "distancia_m": p["distance"],
            "avgGrade": p["avgGrade"],
            "elevGain": p["elevGain"],
            "komElapsedTime": kom,
            "komAthleteId": p.get("komAthleteId"),
            "previsto_grosseiro_s": round(previsto, 1),
            "gap_grosseiro_s": round(previsto - kom, 1),
        })

    antes = len(candidatos)
    candidatos = [c for c in candidatos
                  if c["previsto_grosseiro_s"] <= c["komElapsedTime"] * args.margem]
    candidatos.sort(key=lambda c: c["gap_grosseiro_s"])
    candidatos = candidatos[:args.top]

    print(f"{len(vistos)} segmentos Run na zona ({sem_kom} sem KOM, ignorados; "
          f"{antes} com KOM) -> {len(candidatos)} candidatos após filtro grosseiro "
          f"(margem {args.margem}) e cap top {args.top}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(candidatos, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
