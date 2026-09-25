# -*- coding: utf-8 -*-
"""gap_model.py: Fase 3. Prevê o meu tempo num segmento a partir da curva de
pace GAP do Intervals.icu e da distância efectiva calculada sobre os streams.

    python gap_model.py --in detalhes.json --out previsoes.json --pace-flat 3:40

Grupos (ver avaliar_segmento e o README):
- alta: efectiva >= MIN_DISTANCIA_EFETIVA_M. Tabela da curva ou modelo CS/D'.
  Sai 15-35% mais rápido que os meus PRs e não se corrige: mede o tecto.
- especulativa-plano_subida: mais curto, grade >= 0. Heurística, não física.
- especulativa-descida: mais curto, grade < 0. Sem calibração, não ordena.
- fora_alcance_curva: CS/D' além de FATOR_EXTRAPOLACAO_MAX vezes o fim da
  tabela. Não ordena.
Um segmento nunca corrido que bata o KOM por mais de MARGEM_SUSPEITA_PCT vai
para revisao_manual.

Os pontos da curva acima de FILTRO_VELOCIDADE_MAX_KMH são descartados: uma
corrida com ruído de GPS no arranque já contaminou a tabela abaixo de 900 m.
"""
import argparse
import json
import math
import os
import sys

import requests

from comum import custo_minetti, parse_pace

INTERVALS_BASE = "https://intervals.icu/api/v1"

FILTRO_VELOCIDADE_MAX_KMH = 24.0  # teto plausível para pace sustentado, mesmo curto
MIN_DISTANCIA_EFETIVA_M = 1000.0  # abaixo disto, sem dados credíveis na tabela
MARGEM_SUSPEITA_PCT = 15.0  # vantagem sobre o KOM (nunca corrido) -> revisão manual
FATOR_EXTRAPOLACAO_MAX = 1.5  # cs_model além disto x o máximo da tabela -> fora_alcance_curva


def carregar_env(path=".env"):
    """Lê pares CHAVE=valor de um .env simples (sem dependências extra)."""
    env = dict(os.environ)
    if os.path.exists(path):
        for linha in open(path, encoding="utf-8"):
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                k, v = linha.split("=", 1)
                env.setdefault(k, v)
    return env


def obter_curva_gap(api_key, athlete_id, janela="180d", tipo="Run"):
    """Curva GAP do Intervals.icu (Basic Auth: user 'API_KEY', password a
    chave). Devolve o primeiro item de 'list', já filtrado."""
    r = requests.get(
        f"{INTERVALS_BASE}/athlete/{athlete_id}/pace-curves.json",
        auth=("API_KEY", api_key),
        params={"curves": [janela], "type": tipo, "gap": "true"},
        timeout=30,
    )
    r.raise_for_status()
    lista = r.json().get("list", [])
    if not lista:
        raise SystemExit(f"Curva GAP vazia para janela={janela} tipo={tipo} — "
                          "sem esforços suficientes no período?")
    return filtrar_pontos_implausiveis(lista[0])


def filtrar_pontos_implausiveis(curva):
    """Descarta pontos com velocidade implícita acima de FILTRO_VELOCIDADE_MAX_KMH."""
    teto_ms = FILTRO_VELOCIDADE_MAX_KMH / 3.6
    dists, acts = curva["distance"], curva.get("activity_id", [])
    acts = acts + ["?"] * (len(dists) - len(acts))
    limpos, descartados = [], []
    for p in zip(dists, curva["values"], acts):
        (descartados if p[1] > 0 and p[0] / p[1] > teto_ms else limpos).append(p)
    if descartados:
        fontes = sorted(set(a for _, _, a in descartados))
        print(f"  [aviso] {len(descartados)} pontos da curva descartados "
              f"(velocidade > {FILTRO_VELOCIDADE_MAX_KMH}km/h implausível), "
              f"de {descartados[0][0]:.0f}m a {descartados[-1][0]:.0f}m — fonte(s): {', '.join(fontes)}")
    curva = dict(curva)
    for i, k in enumerate(("distance", "values", "activity_id")):
        curva[k] = [p[i] for p in limpos]
    return curva


PASSO_MIN_M = 5.0  # reamostra os streams a este passo mínimo antes de calcular grade


def distancia_efetiva_streams(dist_stream, elev_stream, passo_min_m=PASSO_MIN_M):
    """Distância GAP-efectiva (m): soma o custo de Minetti em passos de pelo
    menos passo_min_m. Os streams da Strava têm pontos a menos de 0.5 m, e a
    essa escala o ruído de elevação dá grades absurdas."""
    total = 0.0
    acc_dd, acc_de = 0.0, 0.0
    for i in range(1, len(dist_stream)):
        dd = dist_stream[i] - dist_stream[i - 1]
        if dd <= 0:
            continue
        de = elev_stream[i] - elev_stream[i - 1]
        acc_dd += dd
        acc_de += de
        if acc_dd >= passo_min_m:
            total += acc_dd * custo_minetti((acc_de / acc_dd) * 100)
            acc_dd, acc_de = 0.0, 0.0
    if acc_dd > 0:
        total += acc_dd * custo_minetti((acc_de / acc_dd) * 100)
    return total


def _interpolar_tabela(distancia_efetiva_m, curva):
    """Interpolação log-log na tabela da curva. None fora do alcance dela."""
    dists = curva["distance"]
    tempos = curva["values"]
    if distancia_efetiva_m < dists[0] or distancia_efetiva_m > dists[-1]:
        return None
    for i in range(1, len(dists)):
        if dists[i] >= distancia_efetiva_m:
            d0, d1 = dists[i - 1], dists[i]
            t0, t1 = tempos[i - 1], tempos[i]
            if d0 == d1 or t0 <= 0 or t1 <= 0:
                return t1
            frac = (math.log(distancia_efetiva_m) - math.log(d0)) / (math.log(d1) - math.log(d0))
            return math.exp(math.log(t0) + frac * (math.log(t1) - math.log(t0)))
    return tempos[-1]


def modelo_cs(curva):
    """O modelo de tipo CS em paceModels, ou None."""
    return next((m for m in curva.get("paceModels") or [] if m.get("type") == "CS"), None)


def _modelo_cs(distancia_efetiva_m, curva):
    """Modelo critical speed: t = (distancia - D') / CS. None se distancia <= D'."""
    cs_model = modelo_cs(curva)
    if not cs_model:
        return None
    cs = cs_model["criticalSpeed"]  # m/s
    d_prime = cs_model["dPrime"]  # m
    if distancia_efetiva_m <= d_prime:
        return None
    return (distancia_efetiva_m - d_prime) / cs


def extrapolado_demais(distancia_efetiva_m, curva, fator_max=FATOR_EXTRAPOLACAO_MAX):
    """True se a distância passa fator_max vezes o máximo da tabela. CS/D' é
    uma recta de 2 parâmetros e não aguenta ultras (fadiga)."""
    return distancia_efetiva_m > curva["distance"][-1] * fator_max


def prever_tempo(distancia_efetiva_m, curva):
    """Previsão de confiança alta (s): tabela dentro do alcance dela, senão CS/D'."""
    t = _interpolar_tabela(distancia_efetiva_m, curva)
    if t is not None:
        return t, "tabela"
    t = _modelo_cs(distancia_efetiva_m, curva)
    if t is not None:
        return t, "cs_model"
    return None, "fora_de_alcance"


def heuristica_curta(distancia_efetiva_m, curva, pace_flat_s_km=None, previsto_grosseiro_s=None):
    """Estimativa para segmentos curtos, nunca física. Por ordem: pontos reais
    da curva nessa gama, distância efectiva x pace_flat, previsto_grosseiro_s
    da Fase 1. Devolve (valor ou None, metodo)."""
    t = _interpolar_tabela(distancia_efetiva_m, curva)
    if t is not None:
        return t, "curva_gap_curta"
    if pace_flat_s_km is not None:
        return distancia_efetiva_m / 1000.0 * pace_flat_s_km, "grade_efetiva+pace_flat"
    if previsto_grosseiro_s is not None:
        return previsto_grosseiro_s, "grade_media_fase1"
    return None, "sem_dados"


def _suspeito(valor_previsto, kom_tempo_s, ja_corri, margem_pct=MARGEM_SUSPEITA_PCT):
    """Bater o KOM por muito num segmento nunca corrido é mais provável erro
    do modelo. Não se aplica a segmentos já corridos."""
    if valor_previsto is None or not kom_tempo_s or ja_corri:
        return False, None
    vantagem_pct = (kom_tempo_s - valor_previsto) / kom_tempo_s * 100
    if vantagem_pct > margem_pct:
        return True, f"previsão bate o KOM por {vantagem_pct:.0f}% num segmento nunca corrido"
    return False, None


def avaliar_segmento(distancia_efetiva_m, curva, avg_grade_pct, kom_tempo_s, ja_corri,
                      pace_flat_s_km=None, previsto_grosseiro_s=None):
    """Decide o grupo e aplica o guarda-rail de suspeita. descida e
    fora_alcance_curva nunca são suspeitos: o número já não tem base."""
    av = {"previsto_s": None, "metodo_previsao": None, "heuristica_s": None,
          "heuristica_metodo": None, "suspeito": False, "suspeito_motivo": None}
    if distancia_efetiva_m >= MIN_DISTANCIA_EFETIVA_M:
        previsto, metodo = prever_tempo(distancia_efetiva_m, curva)
        av.update(previsto_s=previsto, metodo_previsao=metodo)
        if metodo == "cs_model" and extrapolado_demais(distancia_efetiva_m, curva):
            av["grupo"] = "fora_alcance_curva"
            return av
        av["grupo"] = "alta"
    else:
        heur, metodo = heuristica_curta(distancia_efetiva_m, curva,
                                         pace_flat_s_km=pace_flat_s_km,
                                         previsto_grosseiro_s=previsto_grosseiro_s)
        av.update(heuristica_s=heur, heuristica_metodo=metodo)
        if avg_grade_pct is not None and avg_grade_pct < 0:
            av["grupo"] = "especulativa-descida"
            return av
        av["grupo"] = "especulativa-plano_subida"
    av["suspeito"], av["suspeito_motivo"] = _suspeito(
        av["previsto_s"] if av["grupo"] == "alta" else av["heuristica_s"], kom_tempo_s, ja_corri)
    return av


def avaliar_detalhe(det, curva, pace_flat_s_km, previsto_grosseiro_s):
    """avaliar_segmento sobre uma página de detalhe, mais efetiva, valor e
    gap_kom (valor - KOM). None se não houver streams."""
    streams = det.get("streams") or {}
    dist_s, elev_s = streams.get("distance"), streams.get("elevation")
    if not dist_s or not elev_s:
        print(f"  {det['nome']}: sem streams, salto.")
        return None
    efetiva = distancia_efetiva_streams(dist_s, elev_s)
    kom = det.get("kom_tempo_s")
    av = avaliar_segmento(efetiva, curva, avg_grade_pct=det.get("avgGrade"),
                           kom_tempo_s=kom, ja_corri=det.get("ja_corri", False),
                           pace_flat_s_km=pace_flat_s_km,
                           previsto_grosseiro_s=previsto_grosseiro_s)
    valor = av["previsto_s"] if av["grupo"] == "alta" else av["heuristica_s"]
    av.update(efetiva=efetiva, valor=valor,
              gap_kom=round(valor - kom, 1) if (valor is not None and kom) else None)
    return av


def r1(x):
    return round(x, 1) if x is not None else None


SAIDA = {"alta": "confianca_alta",
         "especulativa-plano_subida": "confianca_especulativa_plano_subida",
         "especulativa-descida": "confianca_especulativa_descida_SEM_CONFIANCA",
         "fora_alcance_curva": "fora_alcance_curva_SEM_CONFIANCA"}


def escrever_grupos(path, itens, chave, extra="", cauda=""):
    """Escreve os 5 grupos em path e imprime o resumo. itens: [(entrada, suspeito)].
    Só alta e plano_subida são ordenados por chave: os outros não são ranking."""
    g = {k: [] for k in [*SAIDA.values(), "revisao_manual"]}
    for x, suspeito in itens:
        g["revisao_manual" if suspeito else SAIDA[x["grupo"]]].append(x)
    for k in ("confianca_alta", "confianca_especulativa_plano_subida"):
        g[k].sort(key=lambda x: (x[chave] is None, x[chave]))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False, indent=1)
    n = [len(v) for v in g.values()]
    print(f"-> {path} ({n[0]} alta, {n[1]} especulativa-plano/subida, "
          f"{n[2]} especulativa-descida SEM CONFIANÇA, "
          f"{n[3]} fora do alcance da curva SEM CONFIANÇA, "
          f"{n[4]} p/ revisão manual{extra}){cauda}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="entrada", default="detalhes.json",
                     help="output do segment_detail.py (precisa de 'streams' por segmento)")
    ap.add_argument("--out", default="previsoes.json")
    ap.add_argument("--janela", default="180d", help="janela da curva GAP (default 180d)")
    ap.add_argument("--pace-flat", dest="pace_flat",
                     help="mm:ss/km, para a heurística dos segmentos curtos (opcional)")
    args = ap.parse_args()

    pace_flat_s_km = parse_pace(args.pace_flat) if args.pace_flat else None

    env = carregar_env()
    api_key = env.get("INTERVALS_ICU_API_KEY", "").strip()
    athlete_id = env.get("INTERVALS_ICU_ATHLETE_ID", "").strip()
    if not api_key or not athlete_id:
        sys.exit("INTERVALS_ICU_API_KEY / INTERVALS_ICU_ATHLETE_ID não definidos "
                  "(env ou .env local).")

    curva = obter_curva_gap(api_key, athlete_id, janela=args.janela)
    cs = modelo_cs(curva)
    if not cs:
        sys.exit("A curva GAP do Intervals.icu não traz modelo CS em paceModels.")
    print(f"curva GAP '{curva['label']}': {curva['days']} dias, "
          f"{len(curva['distance'])} pontos, "
          f"CS={cs['criticalSpeed']:.3f} m/s D'={cs['dPrime']:.1f}m r2={cs['r2']:.4f}")

    detalhes = json.load(open(args.entrada, encoding="utf-8"))
    itens = []
    for d in detalhes:
        av = avaliar_detalhe(d, curva, pace_flat_s_km, d.get("previsto_grosseiro_s"))
        if av is None:
            continue
        kom, efetiva, valor = d.get("kom_tempo_s"), av["efetiva"], av["valor"]
        base = {
            "segmentId": d["segmentId"],
            "nome": d["nome"],
            "distancia_m": d["distancia_m"],
            "distancia_efetiva_gap_m": round(efetiva, 1),
            "kom_tempo_s": kom,
            "grupo": av["grupo"],
            "previsto_s": r1(av["previsto_s"]),
            "metodo_previsao": av["metodo_previsao"],
            "heuristica_s": r1(av["heuristica_s"]),
            "heuristica_metodo": av["heuristica_metodo"],
            "gap_para_kom_s": av["gap_kom"],
            "suspeito_motivo": av["suspeito_motivo"],
        }
        tag = "SUSPEITO" if av["suspeito"] else av["grupo"]
        if valor is not None:
            metodo = av["metodo_previsao"] or av["heuristica_metodo"]
            print(f"  [{tag}] {d['nome']}: efetiva {efetiva:.0f}m -> "
                  f"{valor:.0f}s ({metodo}), KOM {kom}s")
        else:
            print(f"  [{tag}] {d['nome']}: efetiva {efetiva:.0f}m -> sem valor")

        itens.append((base, av["suspeito"]))

    escrever_grupos(args.out, itens, "gap_para_kom_s")


if __name__ == "__main__":
    main()
