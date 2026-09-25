# -*- coding: utf-8 -*-
"""rank.py: Fase 4. Corre as Fases 2 e 3 por segmento e guarda o resultado
em historico.json, para não reanalisar tudo sempre.

    STRAVA_SESSION=<cookie> python rank.py --in candidatos.json \
        --out ranking.json --pace-flat 3:40

Só refaz um segmento se é novo, se o KOM mudou (o tile já o traz, não custa
pedidos) ou se passaram --revisao-semanas. Senão usa o score guardado.
Output nos mesmos 5 grupos do gap_model.py.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from comum import PAGE_DELAY, parse_pace, sessao_strava
from gap_model import avaliar_detalhe, carregar_env, escrever_grupos, obter_curva_gap, r1
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
    """Decide se vale um pedido novo. Devolve (recalcular, motivo)."""
    if entrada is None:
        return True, "novo"
    if entrada.get("kom_tempo_s") != kom_atual:
        return True, "kom_mudou"
    ultima = datetime.fromisoformat(entrada["ultima_analise"])
    if datetime.now(timezone.utc) - ultima >= timedelta(weeks=revisao_semanas):
        return True, "revisao_periodica"
    return False, "cache"


def avaliar_e_persistir(s, curva, c, pace_flat_s_km, motivo):
    """Fases 2 e 3 para um candidato. Devolve a entrada de histórico, ou None
    se não houver streams."""
    det = detalhe_segmento(s, c["segmentId"])
    av = avaliar_detalhe(det, curva, pace_flat_s_km, c.get("previsto_grosseiro_s"))
    if av is None:
        return None
    return {
        "segmentId": c["segmentId"],
        "nome": det["nome"],
        "distancia_m": det["distancia_m"],
        "distancia_efetiva_gap_m": round(av["efetiva"], 1),
        "kom_tempo_s": det.get("kom_tempo_s"),
        "kom_atleta": det.get("kom_atleta"),
        "ja_corri": det.get("ja_corri"),
        "grupo": av["grupo"],
        "previsto_s": r1(av["previsto_s"]),
        "heuristica_s": r1(av["heuristica_s"]),
        "metodo": av["metodo_previsao"] or av["heuristica_metodo"],
        "score": av["gap_kom"],
        "suspeito": av["suspeito"],
        "suspeito_motivo": av["suspeito_motivo"],
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
                     help="mm:ss/km, para a heurística dos segmentos curtos")
    ap.add_argument("--revisao-semanas", type=int, default=REVISAO_SEMANAS_DEFAULT)
    ap.add_argument("--janela", default="180d", help="janela da curva GAP (default 180d)")
    ap.add_argument("--max-novos", type=int, default=30,
                     help="máx. de segmentos a (re)analisar nesta corrida (default 30)")
    args = ap.parse_args()

    pace_flat_s_km = parse_pace(args.pace_flat) if args.pace_flat else None

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
            tag = "SUSPEITO" if entrada["suspeito"] else entrada["grupo"]
            print(f"  [{motivo}/{tag}] {entrada['nome']}: score {entrada['score']}")
            if i < len(a_reanalisar) - 1:
                time.sleep(PAGE_DELAY)

    guardar_historico(historico, args.historico)

    escrever_grupos(args.out, [(x, x.get("suspeito")) for x in reaproveitados + novos], "score",
                    extra=f", {len(novos)} novo/actualizado, {len(reaproveitados)} do cache",
                    cauda=f" | histórico -> {args.historico}")


if __name__ == "__main__":
    main()
