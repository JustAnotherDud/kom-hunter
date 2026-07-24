# -*- coding: utf-8 -*-
"""gap_model.py — Fase 3: previsão do meu tempo num segmento, a partir da
curva de pace GAP do Intervals.icu (API key directa, não MCP) + distância
efectiva calculada ponto-a-ponto sobre os streams do segmento (Fase 2).

Dois passos, mais precisos que o filtro grosseiro da Fase 1 (que só usava
avgGrade):
1. distancia_efetiva_streams() — soma o custo de Minetti grade-a-grade
   entre pontos consecutivos do stream (não a grade média do segmento).
2. prever_tempo() — interpola essa distância efectiva na curva GAP real
   (Intervals.icu já devolve uma curva "distância -> melhor tempo" já
   normalizada por grade, quando pedida com gap=true), com o modelo de
   critical speed (CS/D') como reforço fora do alcance da tabela.

Validação (24 Jul 2026):
- 1ª tentativa contra 2 segmentos já corridos (94s, 64s, ambos <500m
  efectivos): falhou feio (-41%, -63%). Causa: a tabela distance[]/values[]
  do Intervals.icu, entre ~45m e ~900m, vinha inteira de UMA corrida (EDP
  Lisbon Half Marathon 10K, 8 Mar 2026, activity i143235762) com ruído de
  GPS no arranque — implica 41-55 km/h, fisicamente impossível.
- Com o guarda-rail de velocidade (ver baixo), testado contra 5 segmentos
  reais ≥1000m efectivos: erro sistemático de -15% a -35% (previsão sempre
  mais rápida que o PR real). Decisão: NÃO corrigir — a curva GAP mede o
  meu tecto de capacidade (melhor esforço já feito), não o ritmo casual de
  treino, e é essa a pergunta da ferramenta ("se for a sério, consigo bater
  o KOM?"). PRs de corridas de treino normais não são esforços máximos
  dirigidos ao troço, por isso um viés nessa direcção é esperado.

Duas confianças distintas no output, por decisão explícita (não fingir
precisão que o modelo não tem para segmentos curtos):
- confianca="alta" (efectiva >= MIN_DISTANCIA_EFETIVA_M): previsto_s vem do
  modelo GAP (tabela interpolada ou CS/D'), sem correcção.
- confianca="especulativa" (efectiva < MIN_DISTANCIA_EFETIVA_M): a curva
  GAP não tem cobertura real aqui (treino de fundo não gera dados de
  sprint estruturado — confirmado: todos os pontos <900m vinham da mesma
  corrida contaminada). previsto_s fica None; heuristica_s usa dados reais
  curtos que tenham sobrevivido ao filtro (se existirem) ou, na falta
  deles, grade efectiva + pace de referência (--pace-flat) ou o
  previsto_grosseiro_s já carregado desde a Fase 1. heuristica_metodo diz
  sempre qual foi usado — nunca finge ser previsão física.

Guarda-rail permanente (não é só para hoje): FILTRO_VELOCIDADE_MAX_KMH
descarta pontos da tabela cuja velocidade implícita ultrapasse um teto
fisiologicamente plausível, ANTES de qualquer cálculo — se outra corrida
futura tiver ruído GPS parecido, isto evita engolir o ponto em silêncio
(fica um aviso na consola).
"""
import argparse
import json
import math
import os
import sys

import requests

from comum import custo_minetti

INTERVALS_BASE = "https://intervals.icu/api/v1"

FILTRO_VELOCIDADE_MAX_KMH = 24.0  # teto plausível para pace sustentado, mesmo curto
MIN_DISTANCIA_EFETIVA_M = 1000.0  # abaixo disto, sem dados credíveis na tabela (ver acima)


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
    """Curva de pace GAP-normalizada do Intervals.icu (API key directa,
    Basic Auth: username literal 'API_KEY', password a chave — confirmado
    no fórum oficial). Devolve o dict bruto do primeiro item de 'list'."""
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
    """Descarta pontos da tabela distance[]/values[] cuja velocidade
    implícita ultrapassa FILTRO_VELOCIDADE_MAX_KMH — guarda-rail contra
    ruído de GPS/pace em atividades-fonte (ver docstring do módulo)."""
    teto_ms = FILTRO_VELOCIDADE_MAX_KMH / 3.6
    dists, tempos, acts = curva["distance"], curva["values"], curva.get("activity_id", [])
    limpos_d, limpos_t, limpos_a = [], [], []
    descartados = []
    for i in range(len(dists)):
        if tempos[i] > 0 and dists[i] / tempos[i] > teto_ms:
            descartados.append((dists[i], tempos[i], acts[i] if i < len(acts) else "?"))
            continue
        limpos_d.append(dists[i])
        limpos_t.append(tempos[i])
        limpos_a.append(acts[i] if i < len(acts) else "?")
    if descartados:
        fontes = sorted(set(a for _, _, a in descartados))
        print(f"  [aviso] {len(descartados)} pontos da curva descartados "
              f"(velocidade > {FILTRO_VELOCIDADE_MAX_KMH}km/h implausível), "
              f"de {dists[0]:.0f}m a {descartados[-1][0]:.0f}m — fonte(s): {', '.join(fontes)}")
    curva = dict(curva)
    curva["distance"], curva["values"], curva["activity_id"] = limpos_d, limpos_t, limpos_a
    return curva


PASSO_MIN_M = 5.0  # reamostra os streams a este passo mínimo antes de calcular grade


def distancia_efetiva_streams(dist_stream, elev_stream, passo_min_m=PASSO_MIN_M):
    """Distância GAP-efectiva (m), somando o custo de Minetti sobre passos
    reamostrados a >= passo_min_m. Necessário: os streams da Strava vêm com
    espaçamento irregular, por vezes <0.5m entre pontos — nessa escala, o
    ruído normal de GPS/altímetro (dezenas de cm) implica grades de
    centenas de % que o custo_minetti() do comum.py já limita, mas ainda
    assim distorcem o resultado se não forem primeiro agregados a uma
    distância onde a grade calculada faz sentido físico."""
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
    """Interpolação log-log na tabela distance[]/values[] da curva —
    só válida dentro do alcance da tabela (devolve None fora dele)."""
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


def _modelo_cs(distancia_efetiva_m, curva):
    """Modelo critical speed (CS/D'): t = (distancia - D') / CS.
    Usado como reforço fora do alcance da tabela — degrada para distâncias
    muito curtas (perto ou abaixo de D')."""
    modelos = curva.get("paceModels") or []
    cs_model = next((m for m in modelos if m.get("type") == "CS"), None)
    if not cs_model:
        return None
    cs = cs_model["criticalSpeed"]  # m/s
    d_prime = cs_model["dPrime"]  # m
    if distancia_efetiva_m <= d_prime:
        return None  # fora do domínio razoável do modelo
    return (distancia_efetiva_m - d_prime) / cs


def prever_tempo(distancia_efetiva_m, curva):
    """Previsão de confiança alta (segundos): só chamar com
    distancia_efetiva_m >= MIN_DISTANCIA_EFETIVA_M (ver avaliar_segmento).
    Tabela interpolada quando a distância cai no alcance observado, senão
    o modelo CS/D' como fallback."""
    if distancia_efetiva_m < MIN_DISTANCIA_EFETIVA_M:
        return None, "curto_demais"
    t = _interpolar_tabela(distancia_efetiva_m, curva)
    if t is not None:
        return t, "tabela"
    t = _modelo_cs(distancia_efetiva_m, curva)
    if t is not None:
        return t, "cs_model"
    return None, "fora_de_alcance"


def heuristica_curta(distancia_efetiva_m, curva, avg_grade_pct=None, pace_flat_s_km=None,
                      previsto_grosseiro_s=None):
    """Estimativa para segmentos <MIN_DISTANCIA_EFETIVA_M — nunca uma
    previsão física, sempre marcada como tal. Ordem de preferência:
    1. Pontos reais da própria curva GAP que sobrevivam ao filtro de
       velocidade nessa gama curta (se algum dia houver dados de sprint
       estruturado no Intervals.icu, isto passa a usá-los automaticamente).
    2. --pace-flat explícito × distância efectiva já calculada com os
       streams do segmento (mais fino que a Fase 1, que só usava a grade
       média).
    3. previsto_grosseiro_s já carregado da Fase 1 (grade média + pace-flat
       dado nesse momento) — pior aproximação, mas melhor que nada.
    Devolve (valor_ou_None, metodo)."""
    t = _interpolar_tabela(distancia_efetiva_m, curva)
    if t is not None:
        return t, "curva_gap_curta"
    if pace_flat_s_km is not None:
        return distancia_efetiva_m / 1000.0 * pace_flat_s_km, "grade_efetiva+pace_flat"
    if previsto_grosseiro_s is not None:
        return previsto_grosseiro_s, "grade_media_fase1"
    return None, "sem_dados"


def avaliar_segmento(distancia_efetiva_m, curva, pace_flat_s_km=None, previsto_grosseiro_s=None):
    """Ponto único de decisão confianca alta/especulativa. Devolve dict
    com previsto_s (só se confianca=alta), heuristica_s + heuristica_metodo
    (só se confianca=especulativa) e confianca."""
    if distancia_efetiva_m >= MIN_DISTANCIA_EFETIVA_M:
        previsto, metodo = prever_tempo(distancia_efetiva_m, curva)
        return {"confianca": "alta", "previsto_s": previsto, "metodo_previsao": metodo,
                "heuristica_s": None, "heuristica_metodo": None}
    heur, metodo = heuristica_curta(distancia_efetiva_m, curva,
                                     pace_flat_s_km=pace_flat_s_km,
                                     previsto_grosseiro_s=previsto_grosseiro_s)
    return {"confianca": "especulativa", "previsto_s": None, "metodo_previsao": None,
            "heuristica_s": heur, "heuristica_metodo": metodo}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="entrada", default="detalhes.json",
                     help="output do segment_detail.py (precisa de 'streams' por segmento)")
    ap.add_argument("--out", default="previsoes.json")
    ap.add_argument("--janela", default="180d", help="janela da curva GAP (default 180d)")
    ap.add_argument("--pace-flat", dest="pace_flat",
                     help="mm:ss/km — refina a heurística especulativa para segmentos curtos "
                          "(opcional; sem isto usa-se previsto_grosseiro_s da Fase 1, se existir)")
    args = ap.parse_args()

    pace_flat_s_km = None
    if args.pace_flat:
        m, sec = args.pace_flat.split(":")
        pace_flat_s_km = int(m) * 60 + int(sec)

    env = carregar_env()
    api_key = env.get("INTERVALS_ICU_API_KEY", "").strip()
    athlete_id = env.get("INTERVALS_ICU_ATHLETE_ID", "").strip()
    if not api_key or not athlete_id:
        sys.exit("INTERVALS_ICU_API_KEY / INTERVALS_ICU_ATHLETE_ID não definidos "
                  "(env ou .env local).")

    curva = obter_curva_gap(api_key, athlete_id, janela=args.janela)
    print(f"curva GAP '{curva['label']}': {curva['days']} dias, "
          f"{len(curva['distance'])} pontos, "
          f"CS={curva['paceModels'][0]['criticalSpeed']:.3f} m/s "
          f"D'={curva['paceModels'][0]['dPrime']:.1f}m "
          f"r2={curva['paceModels'][0]['r2']:.4f}")

    detalhes = json.load(open(args.entrada, encoding="utf-8"))
    alta, especulativa = [], []
    for d in detalhes:
        streams = d.get("streams") or {}
        dist_s, elev_s = streams.get("distance"), streams.get("elevation")
        if not dist_s or not elev_s:
            print(f"  {d['nome']}: sem streams, salto.")
            continue
        efetiva = distancia_efetiva_streams(dist_s, elev_s)
        av = avaliar_segmento(efetiva, curva, pace_flat_s_km=pace_flat_s_km,
                               previsto_grosseiro_s=d.get("previsto_grosseiro_s"))
        kom = d.get("kom_tempo_s")
        base = {
            "segmentId": d["segmentId"],
            "nome": d["nome"],
            "distancia_m": d["distancia_m"],
            "distancia_efetiva_gap_m": round(efetiva, 1),
            "kom_tempo_s": kom,
            "confianca": av["confianca"],
        }
        if av["confianca"] == "alta":
            previsto = av["previsto_s"]
            base["previsto_s"] = round(previsto, 1) if previsto is not None else None
            base["metodo_previsao"] = av["metodo_previsao"]
            base["gap_para_kom_s"] = (round(previsto - kom, 1)
                                       if (previsto is not None and kom) else None)
            alta.append(base)
            if previsto is not None:
                print(f"  [alta] {d['nome']}: efetiva {efetiva:.0f}m -> "
                      f"previsto {previsto:.0f}s ({av['metodo_previsao']}), KOM {kom}s")
            else:
                print(f"  [alta] {d['nome']}: efetiva {efetiva:.0f}m -> "
                      f"sem previsão ({av['metodo_previsao']})")
        else:
            heur = av["heuristica_s"]
            base["heuristica_s"] = round(heur, 1) if heur is not None else None
            base["heuristica_metodo"] = av["heuristica_metodo"]
            base["heuristica_gap_para_kom_s"] = (round(heur - kom, 1)
                                                  if (heur is not None and kom) else None)
            especulativa.append(base)
            if heur is not None:
                print(f"  [especulativa] {d['nome']}: efetiva {efetiva:.0f}m -> "
                      f"heurística {heur:.0f}s ({av['heuristica_metodo']}), KOM {kom}s")
            else:
                print(f"  [especulativa] {d['nome']}: efetiva {efetiva:.0f}m -> "
                      f"sem heurística ({av['heuristica_metodo']})")

    alta.sort(key=lambda x: (x["gap_para_kom_s"] is None, x["gap_para_kom_s"]))
    especulativa.sort(key=lambda x: (x["heuristica_gap_para_kom_s"] is None,
                                      x["heuristica_gap_para_kom_s"]))

    saida = {"confianca_alta": alta, "confianca_especulativa": especulativa}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=1)
    print(f"-> {args.out} ({len(alta)} confiança alta, {len(especulativa)} especulativa)")


if __name__ == "__main__":
    main()
