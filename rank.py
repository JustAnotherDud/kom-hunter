# -*- coding: utf-8 -*-
"""rank.py — Fase 4: orquestrador final, com histórico persistente por
segmento (historico.json) para não reanalisar tudo sempre.

    STRAVA_SESSION=<cookie> python rank.py --in candidatos.json \
        --out ranking.json --pace-flat 3:40

Por segmento, só refaz a Fase 2 (detalhe/streams) + Fase 3 (previsão) se:
- ainda não está em historico.json ("novo"), ou
- o KOM mudou desde a última análise ("kom_mudou" — comparado de graça,
  o candidatos.json da Fase 1 já traz komElapsedTime do tile, sem pedido
  extra à Strava), ou
- passaram >= --revisao-semanas desde a última análise ("revisao_periodica"
  — a minha própria capacidade evolui, não só o KOM dos outros).
Caso contrário reaproveita o score guardado, zero pedidos novos.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from comum import PAGE_DELAY, sessao_strava
from gap_model import avaliar_segmento, carregar_env, distancia_efetiva_streams, obter_curva_gap
from segment_detail import detalhe_segmento

HISTORICO_DEFAULT = "historico.json"
REVISAO_SEMANAS_DEFAULT = 4


def carregar_historico(path):
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return {"segmentos": {}}


def guardar_historico(historico, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=1, sort_keys=True)


def precisa_recalcular(entrada, kom_atual, revisao_semanas):
    """Decide se vale a pena gastar um pedido novo. entrada=None -> sempre
    recalcula. Comparar kom_atual não custa nada — já vem no candidatos.json
    da Fase 1 (tile), não requer ida à Strava."""
    if entrada is None:
        return True, "novo"
    if entrada.get("kom_tempo_s") != kom_atual:
        return True, "kom_mudou"
    ultima = datetime.fromisoformat(entrada["ultima_analise"])
    if datetime.now(timezone.utc) - ultima >= timedelta(weeks=revisao_semanas):
        return True, "revisao_periodica"
    return False, "cache"


def avaliar_e_persistir(s, curva, c, pace_flat_s_km, motivo):
    """Fase 2 + Fase 3 para um único candidato, devolve a entrada de
    histórico já pronta a guardar (ou None se o segmento não tiver
    streams utilizáveis)."""
    det = detalhe_segmento(s, c["segmentId"])
    streams = det.get("streams") or {}
    dist_s, elev_s = streams.get("distance"), streams.get("elevation")
    if not dist_s or not elev_s:
        print(f"  {det['nome']}: sem streams, salto.")
        return None

    efetiva = distancia_efetiva_streams(dist_s, elev_s)
    av = avaliar_segmento(efetiva, curva, pace_flat_s_km=pace_flat_s_km,
                           previsto_grosseiro_s=c.get("previsto_grosseiro_s"))
    kom = det.get("kom_tempo_s")

    score = None
    if av["confianca"] == "alta" and av["previsto_s"] is not None and kom:
        score = round(av["previsto_s"] - kom, 1)
    elif av["confianca"] == "especulativa" and av["heuristica_s"] is not None and kom:
        score = round(av["heuristica_s"] - kom, 1)

    return {
        "segmentId": c["segmentId"],
        "nome": det["nome"],
        "distancia_m": det["distancia_m"],
        "distancia_efetiva_gap_m": round(efetiva, 1),
        "kom_tempo_s": kom,
        "kom_atleta": det.get("kom_atleta"),
        "ja_corri": det.get("ja_corri"),
        "confianca": av["confianca"],
        "previsto_s": round(av["previsto_s"], 1) if av["previsto_s"] is not None else None,
        "heuristica_s": round(av["heuristica_s"], 1) if av["heuristica_s"] is not None else None,
        "metodo": av["metodo_previsao"] or av["heuristica_metodo"],
        "score": score,
        "ultima_analise": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "motivo_recalculo": motivo,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="entrada", default="candidatos.json")
    ap.add_argument("--out", default="ranking.json")
    ap.add_argument("--historico", default=HISTORICO_DEFAULT)
    ap.add_argument("--pace-flat", dest="pace_flat",
                     help="mm:ss/km — heurística especulativa <1000m")
    ap.add_argument("--revisao-semanas", type=int, default=REVISAO_SEMANAS_DEFAULT)
    ap.add_argument("--janela", default="180d", help="janela da curva GAP (default 180d)")
    ap.add_argument("--max-novos", type=int, default=30,
                     help="cap de segmentos NOVOS/a-reanalisar nesta corrida "
                          "(cache hits não contam para o cap)")
    args = ap.parse_args()

    pace_flat_s_km = None
    if args.pace_flat:
        m, sec = args.pace_flat.split(":")
        pace_flat_s_km = int(m) * 60 + int(sec)

    candidatos = json.load(open(args.entrada, encoding="utf-8"))
    historico = carregar_historico(args.historico)
    segmentos = historico.setdefault("segmentos", {})

    a_reanalisar, reaproveitados = [], []
    for c in candidatos:
        entrada = segmentos.get(str(c["segmentId"]))
        recalcular, motivo = precisa_recalcular(entrada, c["komElapsedTime"], args.revisao_semanas)
        (a_reanalisar if recalcular else reaproveitados).append(
            (c, motivo) if recalcular else entrada)

    print(f"{len(candidatos)} candidatos: {len(reaproveitados)} do cache, "
          f"{len(a_reanalisar)} a (re)analisar")

    if len(a_reanalisar) > args.max_novos:
        print(f"  cap --max-novos={args.max_novos} — só processo os primeiros "
              f"{args.max_novos} (candidatos.json já vem ordenado pelo gap grosseiro). "
              "Corre outra vez para o resto.")
        a_reanalisar = a_reanalisar[:args.max_novos]

    novos = []
    if a_reanalisar:
        cookie = os.environ.get("STRAVA_SESSION", "").strip()
        if not cookie:
            sys.exit("STRAVA_SESSION não definido (preciso para (re)analisar segmentos).")
        env = carregar_env()
        api_key = env.get("INTERVALS_ICU_API_KEY", "").strip()
        athlete_id = env.get("INTERVALS_ICU_ATHLETE_ID", "").strip()
        if not api_key or not athlete_id:
            sys.exit("INTERVALS_ICU_API_KEY / INTERVALS_ICU_ATHLETE_ID não definidos.")

        s = sessao_strava(cookie)
        curva = obter_curva_gap(api_key, athlete_id, janela=args.janela)

        for i, (c, motivo) in enumerate(a_reanalisar):
            entrada = avaliar_e_persistir(s, curva, c, pace_flat_s_km, motivo)
            if entrada is None:
                continue
            segmentos[str(c["segmentId"])] = entrada
            novos.append(entrada)
            print(f"  [{motivo}] {entrada['nome']}: confiança {entrada['confianca']}, "
                  f"score {entrada['score']}")
            if i < len(a_reanalisar) - 1:
                time.sleep(PAGE_DELAY)

    guardar_historico(historico, args.historico)

    todos = reaproveitados + novos
    alta = sorted((x for x in todos if x["confianca"] == "alta"),
                  key=lambda x: (x["score"] is None, x["score"]))
    especulativa = sorted((x for x in todos if x["confianca"] == "especulativa"),
                           key=lambda x: (x["score"] is None, x["score"]))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"confianca_alta": alta, "confianca_especulativa": especulativa},
                   f, ensure_ascii=False, indent=1)
    print(f"-> {args.out} ({len(alta)} confiança alta, {len(especulativa)} especulativa, "
          f"{len(novos)} novo/actualizado, {len(reaproveitados)} do cache) | "
          f"histórico -> {args.historico}")


if __name__ == "__main__":
    main()
