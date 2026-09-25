# -*- coding: utf-8 -*-
"""explore.py: Fase 1. Procura segmentos Run perto de um ponto, só com os
dados do tile (sem pedir /segments/<id>).

    STRAVA_SESSION=<cookie> python explore.py --lat <LAT> --lon <LON> \
        --athlete-id <ID> --raio 1.5 --pace-flat 3:40

Escreve candidatos.json ordenado por gap_grosseiro_s e cortado a --top.
gap_grosseiro_s usa só a grade média e --pace-flat: ordena, nunca exclui.
Segmentos sem komElapsedTime no tile são dados em falta na Strava (o KOM
existe na página) e vão para --out-sem-kom.

Zoom 15 por defeito: abaixo disso os tiles perdem segmentos. Para áreas
grandes, corre vários centros em vez de subir --raio. Ver README.
"""
import argparse
import json
import math
import os
import sys
import time

from comum import (ACTIVITY_TYPE_RUN, INTENT_DEFAULT, PAGE_DELAY, largura_tile_km,
                    obter_tile_segmentos, parse_pace, sessao_strava, tempo_previsto_grosseiro,
                    tiles_no_raio)

MAX_TILES = 40  # cap de pedidos de tiles por corrida (~raio 2.5 km a zoom 15)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--raio", type=float, default=1.5,
                     help="raio em km (default 1.5). Para mais área, corre vários centros")
    ap.add_argument("--zoom", type=int, default=15,
                     help="zoom das tiles (default 15). Abaixo disso perdem-se segmentos")
    ap.add_argument("--intent", default=INTENT_DEFAULT,
                     help=f"parâmetro intent do tile (default '{INTENT_DEFAULT}' — "
                          "NUNCA 'popular', filtra muito)")
    ap.add_argument("--pace-flat", required=True, dest="pace_flat",
                     help="pace de referência em plano, mm:ss/km")
    ap.add_argument("--athlete-id", default=os.environ.get("STRAVA_ATHLETE_ID"),
                     help="id do atleta da STRAVA_SESSION (ou env STRAVA_ATHLETE_ID)")
    ap.add_argument("--top", type=int, default=40,
                     help="máx. de candidatos no output (default 40)")
    ap.add_argument("--out", default="candidatos.json")
    ap.add_argument("--out-sem-kom", default="sem_kom.json",
                     help="segmentos sem komElapsedTime no tile, para ver à mão")
    args = ap.parse_args()

    cookie = os.environ.get("STRAVA_SESSION", "").strip()
    if not cookie:
        sys.exit("STRAVA_SESSION não definido.")

    if not args.athlete_id:
        sys.exit("--athlete-id (ou env STRAVA_ATHLETE_ID) não definido — obrigatório, e "
                 "tem de corresponder ao atleta do STRAVA_SESSION (o endpoint de tiles "
                 "devolve 401 se não bater certo).")

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
            vistos[p["segmentId"]] = p  # tiles adjacentes repetem segmentos
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
    sem_kom = []
    for sid, p in vistos.items():
        kom = p.get("komElapsedTime")
        if not kom:
            # dados em falta no tile, não "ninguém tentou"
            sem_kom.append({
                "segmentId": sid,
                "nome": p["name"],
                "distancia_m": p["distance"],
                "attemptsAllTime": p.get("attemptsAllTime"),
                "athletesAllTime": p.get("athletesAllTime"),
                "qomElapsedTime": p.get("qomElapsedTime"),
                "url": f"https://www.strava.com/segments/{sid}",
            })
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

    total_com_kom = len(candidatos)
    # a estimativa grosseira só ordena; quem fica de fora decide-o --top
    candidatos.sort(key=lambda c: c["gap_grosseiro_s"])
    candidatos = candidatos[:args.top]

    print(f"{len(vistos)} segmentos Run na zona ({len(sem_kom)} com dados em falta — "
          f"ver {args.out_sem_kom}, NÃO tratar como oportunidade livre; "
          f"{total_com_kom} com KOM) -> {len(candidatos)} candidatos "
          f"(ordenados por gap_grosseiro_s, cortados a --top {args.top})")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(candidatos, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out}")

    if sem_kom:
        with open(args.out_sem_kom, "w", encoding="utf-8") as f:
            json.dump(sem_kom, f, ensure_ascii=False, indent=1)
        print(f"-> {args.out_sem_kom} ({len(sem_kom)} segmento(s) — inspeccionar manualmente)")
        for x in sem_kom:
            print(f"   [dados em falta] {x['nome']}: {x['attemptsAllTime']} tentativas, "
                  f"{x['athletesAllTime']} atletas -> {x['url']}")


if __name__ == "__main__":
    main()
