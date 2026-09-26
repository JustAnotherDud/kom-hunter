# -*- coding: utf-8 -*-
"""segment_detail.py: Fase 2. Detalhe, KOM e streams de cada candidato.

    python segment_detail.py --in candidatos.json --out detalhes.json

Cada segmento é uma página que não visitarias a navegar, por isso há
PAGE_DELAY entre pedidos e um cap por corrida (--max, default 30).
"""
import argparse
import json
import os
import sys
import time

from comum import PAGE_DELAY, carregar_env, obter_next_data, sessao_strava

DEFAULT_MAX = 30


def detalhe_segmento(s, seg_id):
    url = f"https://www.strava.com/segments/{seg_id}"
    d = obter_next_data(s, url)
    pp = d["props"]["pageProps"]
    leaderboard = pp.get("initialLeaderboard", {}).get("leaderboard", [])
    pr = pp.get("athletePrEffort") or {}
    return {
        "segmentId": seg_id,
        "nome": pp["metadata"]["name"],
        "tipo": pp["metadata"]["activityType"],
        "localizacao": pp["metadata"].get("displayLocation", ""),
        "distancia_m": pp["measurements"]["distance"],
        "avgGrade": pp["measurements"]["avgGrade"],
        "elevGain": pp["measurements"]["elevGain"],
        "elevLow": pp["measurements"]["elevLow"],
        "elevHigh": pp["measurements"]["elevHigh"],
        "kom_tempo_s": leaderboard[0]["elapsedTime"] if leaderboard else None,
        "kom_atleta": leaderboard[0]["displayName"] if leaderboard else None,
        "leaderboard_top10": [
            {"rank": l["rank"], "atleta": l["displayName"], "tempo_s": l["elapsedTime"],
             **({"athleteId": l["athleteId"]} if "athleteId" in l else {})}
            for l in leaderboard
        ],
        "ja_corri": pp.get("athleteEffortCount", 0) > 0,
        "athleteEffortCount": pp.get("athleteEffortCount", 0),
        "meu_pr_s": pr.get("timing", {}).get("elapsedTime"),
        "streams": pp.get("streams", {}),
    }


def main():
    carregar_env()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="entrada", default="candidatos.json")
    ap.add_argument("--out", default="detalhes.json")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX,
                     help=f"cap de segmentos por corrida (default {DEFAULT_MAX})")
    ap.add_argument("--so-nao-corridos", action="store_true",
                     help="descarta no fim os que já têm athleteEffortCount > 0")
    args = ap.parse_args()

    cookie = os.environ.get("STRAVA_SESSION", "").strip()
    if not cookie:
        sys.exit("STRAVA_SESSION não definido.")

    candidatos = json.load(open(args.entrada, encoding="utf-8"))
    if len(candidatos) > args.max:
        print(f"{len(candidatos)} candidatos > cap {args.max} — só processo os primeiros "
              f"{args.max} (já vêm ordenados pelo gap grosseiro da Fase 1). "
              "Corre outra vez para o resto.")
        candidatos = candidatos[:args.max]

    s = sessao_strava(cookie)
    detalhes = []
    for i, c in enumerate(candidatos):
        det = detalhe_segmento(s, c["segmentId"])
        detalhes.append(det)
        print(f"  {i + 1}/{len(candidatos)}: {det['nome']} — "
              f"KOM {det['kom_tempo_s']}s, já corri: {det['ja_corri']}")
        if i < len(candidatos) - 1:
            time.sleep(PAGE_DELAY)

    if args.so_nao_corridos:
        antes = len(detalhes)
        detalhes = [d for d in detalhes if not d["ja_corri"]]
        print(f"--so-nao-corridos: {antes} -> {len(detalhes)}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(detalhes, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
