"""Harness de snapshot: corre os scripts com Strava e Intervals.icu falsos
(tests/mocks.py) e compara o output com tests/baseline.txt. Sem rede.

    python tests/harness.py            # compara
    python tests/harness.py --update   # aceita o resultado actual como baseline
"""
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

from mocks import SEGS, install_mocks, next_data

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BASELINE = os.path.join(HERE, "baseline.txt")
NL = chr(10)

# ---------- cenários ----------

ENV_ICU = "INTERVALS_ICU_API_KEY=k\nINTERVALS_ICU_ATHLETE_ID=0\n"


def detalhes_fixos():
    out = []
    for sid in SEGS:
        if not SEGS[sid][3] or SEGS[sid][4] != 9:
            continue
        pp = next_data(sid)["props"]["pageProps"]
        d = {"segmentId": sid, "nome": pp["metadata"]["name"],
             "distancia_m": pp["measurements"]["distance"],
             "avgGrade": pp["measurements"]["avgGrade"],
             "kom_tempo_s": SEGS[sid][3], "ja_corri": SEGS[sid][5],
             "streams": pp.get("streams", {})}
        out.append(d)
    return out


def candidatos_fixos():
    out = []
    for sid in SEGS:
        n, dist, g, kom, at, _, _ = SEGS[sid]
        if not kom or at != 9:
            continue
        out.append({"segmentId": sid, "nome": n, "distancia_m": float(dist), "avgGrade": g,
                    "elevGain": 1.0, "komElapsedTime": kom, "komAthleteId": 1,
                    "previsto_grosseiro_s": round(dist / 1000 * 220, 1),
                    "gap_grosseiro_s": round(dist / 1000 * 220 - kom, 1)})
    return out


def hist(entries):
    return {"segmentos": {str(k): v for k, v in entries.items()}}


def h_entry(sid, kom, when, **kw):
    e = {"segmentId": sid, "nome": SEGS[sid][0], "distancia_m": float(SEGS[sid][1]),
         "distancia_efetiva_gap_m": 1.0, "kom_tempo_s": kom, "kom_atleta": "X",
         "ja_corri": False, "grupo": "alta", "previsto_s": 1.0, "heuristica_s": None,
         "metodo": "tabela", "score": -3.0, "suspeito": False, "suspeito_motivo": None,
         "ultima_analise": when, "motivo_recalculo": "novo"}
    e.update(kw)
    return e


SESSION = {"STRAVA_SESSION": "cookie"}
EXP = ["explore.py", "--lat", "39.36", "--lon", "-8.95", "--raio", "0.5"]

SCENARIOS = {
    "explore_ok": dict(script=EXP + ["--athlete-id", "1", "--top", "5"], env=SESSION,
                       dotenv=ENV_ICU),
    "explore_all_envid": dict(script=EXP + ["--top", "40", "--out", "c.json"],
                              env={**SESSION, "STRAVA_ATHLETE_ID": "7"}, dotenv=ENV_ICU),
    "explore_intent": dict(script=EXP + ["--athlete-id", "1", "--zoom", "14"],
                           env=SESSION, dotenv=ENV_ICU),
    "explore_semkom_antigo": dict(script=EXP + ["--athlete-id", "1"], env=SESSION,
                                  dotenv=ENV_ICU, mode={"sem_107": 1},
                                  files={"sem_kom.json": [{"segmentId": 1, "velho": True}]}),
    "explore_dotenv": dict(script=EXP, env={"STRAVA_ATHLETE_ID": "7"},
                           dotenv="STRAVA_SESSION=cookie\nSTRAVA_ATHLETE_ID=1\n" + ENV_ICU),
    "explore_no_icu": dict(script=EXP + ["--athlete-id", "1"], env=SESSION),
    "explore_curva_curta_sem_cs": dict(script=EXP + ["--athlete-id", "1"], env=SESSION,
                                       dotenv=ENV_ICU, mode={"curva_curta": 1, "sem_cs": 1}),
    "explore_no_session": dict(script=EXP + ["--athlete-id", "1"], env={}),
    "explore_no_athlete": dict(script=EXP, env=SESSION),
    "explore_too_many": dict(script=["explore.py", "--lat", "39.36", "--lon", "-8.95", "--raio", "5",
                                     "--athlete-id", "1"], env=SESSION),
    "explore_401": dict(script=EXP + ["--athlete-id", "1"], env=SESSION, dotenv=ENV_ICU,
                        mode={"tile401": 1}),
    "detail_ok": dict(script=["segment_detail.py", "--max", "3"], env=SESSION,
                      files={"candidatos.json": candidatos_fixos()}),
    "detail_all_nc": dict(script=["segment_detail.py", "--so-nao-corridos", "--out", "d.json"],
                          env=SESSION, files={"candidatos.json": candidatos_fixos()}),
    "detail_nonext": dict(script=["segment_detail.py"], env=SESSION, mode={"nonext": 1},
                          files={"candidatos.json": candidatos_fixos()}),
    "detail_login": dict(script=["segment_detail.py"], env=SESSION, mode={"login": 1},
                         files={"candidatos.json": candidatos_fixos()}),
    "detail_dotenv": dict(script=["segment_detail.py", "--max", "1"],
                          dotenv="STRAVA_SESSION=cookie\n",
                          files={"candidatos.json": candidatos_fixos()}),
    "detail_no_session": dict(script=["segment_detail.py"], env={},
                              files={"candidatos.json": candidatos_fixos()}),
    "gap_pace": dict(script=["gap_model.py"], dotenv=ENV_ICU,
                     files={"detalhes.json": detalhes_fixos()}),
    # a curva sem o ponto dos 1000 m: pace de reserva interpolado entre 800 e 5000 m
    "gap_sem_1000": dict(script=["gap_model.py"], dotenv=ENV_ICU, mode={"sem_1000": 1},
                         files={"detalhes.json": detalhes_fixos()}),
    # a tabela acaba nos 800 m: pace de reserva pelo modelo CS
    "gap_curva_curta": dict(script=["gap_model.py"], dotenv=ENV_ICU, mode={"curva_curta": 1},
                            files={"detalhes.json": detalhes_fixos()}),
    "gap_janela_out": dict(script=["gap_model.py", "--janela", "90d", "--out", "p.json"], dotenv=ENV_ICU,
                       files={"detalhes.json": detalhes_fixos()}),
    "gap_icu_env": dict(script=["gap_model.py"], env={"INTERVALS_ICU_API_KEY": "k",
                                                       "INTERVALS_ICU_ATHLETE_ID": "0"},
                         files={"detalhes.json": detalhes_fixos()}),
    "gap_no_env": dict(script=["gap_model.py"], files={"detalhes.json": detalhes_fixos()}),
    "gap_curva_vazia": dict(script=["gap_model.py"], dotenv=ENV_ICU, mode={"curva_vazia": 1},
                            files={"detalhes.json": detalhes_fixos()}),
    "gap_sem_cs": dict(script=["gap_model.py"], dotenv=ENV_ICU, mode={"sem_cs": 1},
                       files={"detalhes.json": detalhes_fixos()}),
    "gap_cs_segundo": dict(script=["gap_model.py"], dotenv=ENV_ICU, mode={"cs_segundo": 1},
                           files={"detalhes.json": detalhes_fixos()}),
    "gap_so_outro": dict(script=["gap_model.py"], dotenv=ENV_ICU, mode={"so_outro": 1},
                         files={"detalhes.json": detalhes_fixos()}),
    "gap_ruido_meio": dict(script=["gap_model.py"], dotenv=ENV_ICU, mode={"ruido_meio": 1},
                           files={"detalhes.json": detalhes_fixos()}),
    "rank_new": dict(script=["rank.py"], env=SESSION, dotenv=ENV_ICU,
                     files={"candidatos.json": candidatos_fixos()}),
    "rank_janela_sem_cs": dict(script=["rank.py", "--janela", "30d"], env=SESSION, dotenv=ENV_ICU,
                              mode={"sem_cs": 1}, files={"candidatos.json": candidatos_fixos()}),
    "rank_cache_cap": dict(
        script=["rank.py", "--max-novos", "2", "--historico", "h.json",
                "--revisao-semanas", "3", "--out", "r.json"],
        env=SESSION, dotenv=ENV_ICU,
        files={"candidatos.json": candidatos_fixos(),
               "h.json": hist({
                   101: h_entry(101, 420, "2026-09-20T10:00:00+00:00"),
                   102: h_entry(102, 99, "2026-09-20T10:00:00+00:00"),
                   104: h_entry(104, 600, "2026-09-01T10:00:00+00:00"),
                   105: h_entry(105, 7000, "2026-09-24T10:00:00+00:00", grupo="especulativa-descida"),
                   109: h_entry(109, 3900, "2026-09-24T10:00:00+00:00", suspeito=True,
                                suspeito_motivo="x", score=None),
                   110: h_entry(110, 30, "2026-09-24T10:00:00+00:00",
                                grupo="especulativa-plano_subida", score=2.0),
                   999: h_entry(101, 1, "2026-09-24T10:00:00+00:00"),
               })}),
    "rank_revisao": None,
    "rank_all_cached": dict(
        script=["rank.py"], env={}, dotenv=ENV_ICU,
        files={"candidatos.json": [c for c in candidatos_fixos() if c["segmentId"] in (101, 110)],
               "historico.json": {"segmentos": {
                   "101": h_entry(101, 420, "2026-09-20T10:00:00+00:00", score=5.0),
                   "110": h_entry(110, 30, "2026-09-24T10:00:00+00:00",
                                  grupo="fora_alcance_curva")}}}),
    "rank_pace_mudou": dict(
        script=["rank.py"], env=SESSION, dotenv=ENV_ICU,
        files={"candidatos.json": [c for c in candidatos_fixos()
                                   if c["segmentId"] in (101, 102, 103, 110)],
               "historico.json": hist({
                   101: h_entry(101, 420, "2026-09-24T10:00:00+00:00", pace_reserva_s_km=200),
                   102: h_entry(102, 95, "2026-09-24T10:00:00+00:00",
                                grupo="especulativa-plano_subida"),
                   103: h_entry(103, 80, "2026-09-24T10:00:00+00:00",
                                grupo="especulativa-descida", pace_reserva_s_km=210),
                   110: h_entry(110, 30, "2026-09-24T10:00:00+00:00",
                                grupo="especulativa-plano_subida", pace_reserva_s_km=220),
               })}),
    "rank_dotenv": dict(script=["rank.py", "--max-novos", "1"],
                        dotenv=ENV_ICU + "STRAVA_SESSION=cookie\n",
                        files={"candidatos.json": candidatos_fixos()}),
    "rank_no_session": dict(script=["rank.py"], env={}, dotenv=ENV_ICU,
                            files={"candidatos.json": candidatos_fixos()}),
    "rank_no_icu": dict(script=["rank.py"], env=SESSION,
                        files={"candidatos.json": candidatos_fixos()}),
}

# top 10 previsto: o segmento 101 prevê 461.4 s (461 inteiro), KOM 420
LB_101 = {
    "dentro": [420, 445, 458, 464, 470, 476, 483, 490, 498, 505],  # 4.º
    "empate": [420, 445, 458, 461, 470, 476, 483, 490, 498, 505],  # 4.º empatado
    "fora": [420, 425, 430, 435, 440, 445, 448, 450, 452, 455],  # 6 s para o 10.º
    "incompleto": [420, 430, 445, 450],  # 5.º, sem lugar abaixo
}
for _k, _lb in LB_101.items():
    SCENARIOS[f"rank_top10_{_k}"] = dict(
        script=["rank.py"], env=SESSION, dotenv=ENV_ICU,
        mode={"leaderboard": {101: _lb}},
        files={"candidatos.json": [c for c in candidatos_fixos() if c["segmentId"] == 101]})

# o meu tempo (athleteId 1 = STRAVA_ATHLETE_ID) dentro do top 10 não conta.
# Com ids, 460 é o meu: sem ele, 461 fica em 4.º (com ele seria 5.º).
LB_COM_IDS = [(t, 7000 + i) for i, t in enumerate(LB_101["dentro"])]
LB_EU_DENTRO = [(420, 7000), (445, 7001), (458, 7002), (460, 1), (464, 7004),
                (470, 7005), (476, 7006), (483, 7007), (490, 7008), (498, 7009)]
# mais lento que os outros 9: 10.º ou 11.º, o 11.º não vem na página
LB_EU_INCERTO = [(420, 7000), (425, 7001), (430, 1), (435, 7003), (440, 7004),
                 (445, 7005), (448, 7006), (450, 7007), (452, 7008), (455, 7009)]
for _k, _lb in (("eu_dentro", LB_EU_DENTRO), ("eu_incerto", LB_EU_INCERTO),
                ("ids_sem_eu", LB_COM_IDS)):
    SCENARIOS[f"rank_top10_{_k}"] = dict(
        script=["rank.py"],
        env={**SESSION, "STRAVA_ATHLETE_ID": "1"}, dotenv=ENV_ICU,
        mode={"leaderboard": {101: _lb}},
        files={"candidatos.json": [c for c in candidatos_fixos() if c["segmentId"] == 101]})
SCENARIOS["detail_athlete_id"] = dict(
    script=["segment_detail.py", "--max", "1"], env=SESSION,
    mode={"leaderboard": {101: LB_EU_DENTRO}}, files={"candidatos.json": candidatos_fixos()})

SCENARIOS["rank_revisao"] = dict(SCENARIOS["rank_cache_cap"],
                                 script=["rank.py", "--historico", "h.json", "--revisao-semanas", "3"])


def run_one(name):
    sc = SCENARIOS[name]
    for k in ("STRAVA_SESSION", "STRAVA_ATHLETE_ID", "INTERVALS_ICU_API_KEY",
              "INTERVALS_ICU_ATHLETE_ID"):
        os.environ.pop(k, None)
    os.environ.update(sc.get("env", {}))
    install_mocks(sc.get("mode", {}))
    sys.path.insert(0, REPO)
    script = sc["script"]
    sys.argv = script
    import runpy
    try:
        runpy.run_path(os.path.join(REPO, script[0]), run_name="__main__")
        print("[exit] 0")
    except SystemExit as e:
        print(f"[exit] SystemExit({e.code!r})")
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)[-1]
        print(f"[exit] {type(e).__name__}: {e} (em {tb.name})")


def resumir_streams(txt):
    """Troca cada lista de "streams" por número de pontos + hash dos valores,
    para a baseline não guardar milhares de números. O resto fica igual."""
    if '"streams"' not in txt:
        return txt

    def andar(x):
        if isinstance(x, list):
            for y in x:
                andar(y)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k == "streams" and isinstance(v, dict):
                    x[k] = {s: f"{len(vals)} pontos, sha256 "
                               + hashlib.sha256(json.dumps(vals).encode()).hexdigest()[:16]
                            for s, vals in v.items()}
                else:
                    andar(v)

    dados = json.loads(txt)
    andar(dados)
    return json.dumps(dados, ensure_ascii=False, indent=1)


def gerar():
    chunks = []
    for name, sc in SCENARIOS.items():
        wd = tempfile.mkdtemp(prefix="kh_")
        for fn, data in sc.get("files", {}).items():
            with open(os.path.join(wd, fn), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        if "dotenv" in sc:
            with open(os.path.join(wd, ".env"), "w", encoding="utf-8") as f:
                f.write(sc["dotenv"])
        before = {fn: open(os.path.join(wd, fn), encoding="utf-8").read() for fn in os.listdir(wd)}
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", name],
                           cwd=wd, capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                "PYTHONDONTWRITEBYTECODE": "1"})
        chunks.append(f"===== {name} =====" + NL + "--- stdout" + NL + p.stdout
                      + "--- stderr" + NL + p.stderr)
        for fn in sorted(os.listdir(wd)):
            txt = open(os.path.join(wd, fn), encoding="utf-8").read()
            if before.get(fn) != txt:
                chunks.append(f"--- file {fn}" + NL + resumir_streams(txt) + NL)
        shutil.rmtree(wd)
    return NL.join(chunks)


def main():
    if sys.argv[1:2] == ["--one"]:
        return run_one(sys.argv[2])
    atual = gerar()
    if sys.argv[1:2] == ["--update"]:
        with open(BASELINE, "w", encoding="utf-8", newline=NL) as f:
            f.write(atual)
        return print(f"{len(SCENARIOS)} cenários -> {BASELINE}")
    esperado = open(BASELINE, encoding="utf-8").read()
    if atual == esperado:
        return print(f"OK: {len(SCENARIOS)} cenários iguais à baseline")
    diff = difflib.unified_diff(esperado.splitlines(), atual.splitlines(),
                                "baseline.txt", "atual", lineterm="")
    print(NL.join(list(diff)[:200]))
    sys.exit("DIFERENTE da baseline (python tests/harness.py --update aceita o novo resultado)")


if __name__ == "__main__":
    main()
