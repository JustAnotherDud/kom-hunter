# -*- coding: utf-8 -*-
"""rank.py: Fase 4. Corre as Fases 2 e 3 por segmento e guarda o resultado
em historico.json, para não reanalisar tudo sempre.

    python rank.py --in candidatos.json --out ranking.json

Só refaz um segmento se é novo, se o KOM mudou (o tile já o traz, não custa
pedidos), se passaram --revisao-semanas ou, nos curtos, se o pace de reserva
(o da curva GAP aos 1000 m) mudou. Senão usa o score guardado. Por isso pede
a curva ao Intervals.icu em todas as corridas.
Output nos mesmos 5 grupos do gap_model.py. Cada entrada leva ainda "top10":
onde o tempo previsto entraria no top 10 da página de detalhe (já lida, sem
pedidos a mais). É só informação: não muda grupos, score nem ordenação.
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from comum import PAGE_DELAY, carregar_env, sessao_strava
from gap_model import avaliar_detalhe, escrever_grupos, obter_curva_gap, pace_reserva, r1
from segment_detail import detalhe_segmento

HISTORICO_DEFAULT = "historico.json"
REVISAO_SEMANAS_DEFAULT = 4
TOP_N = 10


def posicao_top10(previsto_s, tempos, completo=None, sem_o_meu=False):
    """Onde o tempo previsto entraria no top 10 do leaderboard. Compara em
    segundos inteiros, como a Strava regista. Só informa: não mexe no grupo
    nem no score. None sem previsão ou sem tempos.
    tempos: os dos outros atletas. completo: se o top 10 da página estava
    cheio (por defeito, 10 tempos). Sem o meu tempo, um top 10 cheio deixa 9
    e o 11.º não vem na página."""
    if previsto_s is None or not tempos:
        return None
    tempos = sorted(tempos)[:TOP_N]
    if completo is None:
        completo = len(tempos) == TOP_N
    p = int(math.floor(previsto_s + 0.5))
    mais_rapidos = [t for t in tempos if t < p]
    mais_lentos = [t for t in tempos if t > p]
    empatados = len(tempos) - len(mais_rapidos) - len(mais_lentos)
    sufixo = ", sem o meu tempo" if sem_o_meu else ""
    out = {"previsto_inteiro_s": p, "n_tempos": len(tempos), "posicao": None,
           "empatado_com": empatados, "s_para_lugar_acima": None,
           "margem_lugar_abaixo_s": None, "fora_top10": False, "s_para_10o": None}
    if completo and not mais_lentos and not empatados:
        if len(tempos) < TOP_N:
            # mais lento que os outros 9: 10.º ou 11.º, depende do 11.º
            out.update(fora_top10=None, s_para_lugar_acima=p - tempos[-1],
                       resumo=f"10.º ou fora do top 10, {p - tempos[-1]} s para o lugar "
                              f"acima (o 11.º não vem na página){sufixo}")
            return out
        out.update(fora_top10=True, s_para_10o=p - tempos[-1],
                   resumo=f"fora do top 10, {p - tempos[-1]} s para o 10.º{sufixo}")
        return out
    out["posicao"] = len(mais_rapidos) + 1
    partes = [f"{out['posicao']}.º"
              + (f" empatado com {empatados} tempo(s)" if empatados else "")]
    if mais_rapidos:
        out["s_para_lugar_acima"] = p - mais_rapidos[-1]
        partes.append(f"{out['s_para_lugar_acima']} s para o lugar acima")
    else:
        partes.append("sem lugar acima")
    if mais_lentos:
        out["margem_lugar_abaixo_s"] = mais_lentos[0] - p
        partes.append(f"{out['margem_lugar_abaixo_s']} s de margem para o lugar abaixo")
    else:
        partes.append("sem lugar abaixo")
    if not completo:
        partes.append(f"top 10 incompleto ({len(tempos)} tempos)")
    out["resumo"] = ", ".join(partes) + sufixo
    return out


def carregar_historico(path):
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return {"segmentos": {}}


def guardar_historico(historico, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=1, sort_keys=True)


def precisa_recalcular(entrada, kom_atual, revisao_semanas, pace_reserva_s_km):
    """Decide se vale um pedido novo. Devolve (recalcular, motivo)."""
    if entrada is None:
        return True, "novo"
    if entrada.get("kom_tempo_s") != kom_atual:
        return True, "kom_mudou"
    # nos curtos a heurística usa o pace de reserva, por isso ele faz parte da cache
    if (entrada.get("grupo", "").startswith("especulativa")
            and entrada.get("pace_reserva_s_km") != pace_reserva_s_km):
        return True, "pace_mudou"
    ultima = datetime.fromisoformat(entrada["ultima_analise"])
    if datetime.now(timezone.utc) - ultima >= timedelta(weeks=revisao_semanas):
        return True, "revisao_periodica"
    return False, "cache"


def avaliar_e_persistir(s, curva, c, pace_reserva_s_km, motivo):
    """Fases 2 e 3 para um candidato. Devolve a entrada de histórico, ou None
    se não houver streams."""
    det = detalhe_segmento(s, c["segmentId"])
    av = avaliar_detalhe(det, curva, pace_reserva_s_km)
    if av is None:
        return None
    # o meu próprio tempo não conta como adversário (entradas sem athleteId ficam)
    lb = det.get("leaderboard_top10", [])
    meu_id = os.environ.get("STRAVA_ATHLETE_ID", "").strip()
    outros = [l for l in lb if not meu_id or str(l.get("athleteId")) != meu_id]
    top10 = posicao_top10(r1(av["valor"]), [l["tempo_s"] for l in outros],
                          completo=len(lb) >= TOP_N, sem_o_meu=len(outros) < len(lb))
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
        "pace_reserva_s_km": pace_reserva_s_km,
        "score": av["gap_kom"],
        "suspeito": av["suspeito"],
        "suspeito_motivo": av["suspeito_motivo"],
        "ultima_analise": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "motivo_recalculo": motivo,
        "top10": top10,
    }


def main():
    carregar_env()
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="entrada", default="candidatos.json")
    ap.add_argument("--out", default="ranking.json")
    ap.add_argument("--historico", default=HISTORICO_DEFAULT)
    ap.add_argument("--revisao-semanas", type=int, default=REVISAO_SEMANAS_DEFAULT)
    ap.add_argument("--janela", default="180d", help="janela da curva GAP (default 180d)")
    ap.add_argument("--max-novos", type=int, default=30,
                     help="máx. de segmentos a (re)analisar nesta corrida (default 30)")
    args = ap.parse_args()

    api_key = os.environ.get("INTERVALS_ICU_API_KEY", "").strip()
    athlete_id = os.environ.get("INTERVALS_ICU_ATHLETE_ID", "").strip()
    if not api_key or not athlete_id:
        sys.exit("INTERVALS_ICU_API_KEY / INTERVALS_ICU_ATHLETE_ID não definidos.")
    curva = obter_curva_gap(api_key, athlete_id, janela=args.janela)
    pace_reserva_s_km = pace_reserva(curva)

    candidatos = json.load(open(args.entrada, encoding="utf-8"))
    historico = carregar_historico(args.historico)
    segmentos = historico.setdefault("segmentos", {})

    a_reanalisar, reaproveitados = [], []
    for c in candidatos:
        entrada = segmentos.get(str(c["segmentId"]))
        recalcular, motivo = precisa_recalcular(entrada, c["komElapsedTime"], args.revisao_semanas,
                                                pace_reserva_s_km)
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

        s = sessao_strava(cookie)

        for i, (c, motivo) in enumerate(a_reanalisar):
            if i:
                time.sleep(PAGE_DELAY)
            entrada = avaliar_e_persistir(s, curva, c, pace_reserva_s_km, motivo)
            if entrada is None:
                continue
            segmentos[str(c["segmentId"])] = entrada
            novos.append(entrada)
            tag = "SUSPEITO" if entrada["suspeito"] else entrada["grupo"]
            print(f"  [{motivo}/{tag}] {entrada['nome']}: score {entrada['score']}")
            if entrada["top10"]:
                print(f"      top 10: {entrada['top10']['resumo']}")

    guardar_historico(historico, args.historico)

    escrever_grupos(args.out, [(x, x.get("suspeito")) for x in reaproveitados + novos], "score",
                    extra=f", {len(novos)} novo/actualizado, {len(reaproveitados)} do cache",
                    cauda=f" | histórico -> {args.historico}")


if __name__ == "__main__":
    main()
