"""Dados sintéticos e mocks de Strava e Intervals.icu para o harness."""
import json
import math

# ---------- dados sintéticos ----------

SEGS = {
    # id: (nome, dist, grade, kom, activityType, ja_corri, streams?)
    101: ("Plano longo", 2000, 0.5, 420, 9, False, True),
    102: ("Subida curta", 400, 4.0, 95, 9, True, True),
    103: ("Descida curta", 500, -5.0, 80, 9, False, True),
    104: ("Suspeito", 1500, 0.0, 600, 9, False, True),
    105: ("Ultra", 30000, 0.2, 7000, 9, False, True),
    106: ("Sem streams", 1200, 1.0, 260, 9, False, False),
    107: ("Sem KOM", 900, 1.0, None, 9, False, True),
    108: ("Caminhada", 800, 0.0, 500, 2, False, True),
    109: ("Longo cs", 18000, 0.0, 3900, 9, True, True),
    110: ("Mini", 150, 1.0, 30, 9, False, True),
}


def streams(dist, grade, sid):
    d, e, out_d, out_e, i = 0.0, 100.0, [0.0], [100.0], 0
    while d < dist:
        step = 0.4 if i % 2 else 4.6
        if i % 17 == 5:
            step = 0.0  # ponto repetido (dd <= 0)
        d = min(dist, d + step)
        e = 100.0 + d * grade / 100.0 + 0.3 * math.sin(i * 0.7 + sid)
        out_d.append(round(d, 2))
        out_e.append(round(e, 2))
        i += 1
    return {"distance": out_d, "elevation": out_e}


def tile_props(sid):
    nome, dist, grade, kom, at, _, _ = SEGS[sid]
    p = {"segmentId": sid, "name": nome, "distance": float(dist), "avgGrade": grade,
         "elevGain": max(0.0, dist * grade / 100.0), "activityType": at,
         "komAthleteId": 5000 + sid, "attemptsAllTime": 10 * sid, "athletesAllTime": 3 * sid,
         "qomElapsedTime": 999}
    if kom:
        p["komElapsedTime"] = kom
    return p


def next_data(sid, tempos=None):
    """tempos: substitui o leaderboard por defeito (3 tempos a partir do KOM)."""
    nome, dist, grade, kom, at, ja, has_streams = SEGS[sid]
    if tempos is None:
        tempos = [] if not kom else [kom + 7 * r for r in range(3)]
    lb = [{"rank": r + 1, "displayName": f"Atleta {r}", "elapsedTime": t}
          for r, t in enumerate(tempos)]
    pp = {
        "metadata": {"name": nome, "activityType": "Run", "displayLocation": "Algures"},
        "measurements": {"distance": float(dist), "avgGrade": grade, "elevGain": 5.0,
                         "elevLow": 90.0, "elevHigh": 120.0},
        "initialLeaderboard": {"leaderboard": lb},
        "athleteEffortCount": 2 if ja else 0,
        "athletePrEffort": {"timing": {"elapsedTime": (kom or 100) + 5}} if ja else None,
    }
    if has_streams:
        pp["streams"] = streams(dist, grade, sid)
    return {"props": {"pageProps": pp}}


CURVA = {
    "label": "180d", "days": 180,
    "distance": [100, 200, 400, 800, 1000, 5000, 10000, 15000],
    "values": [12, 35, 75, 160, 205, 1150, 2400, 3700],
    "activity_id": ["iA", "iB", "iB", "iC", "iC", "iD", "iE", "iF"],
    "paceModels": [{"type": "CS", "criticalSpeed": 4.1, "dPrime": 180.0, "r2": 0.9987}],
}

# ---------- mocks ----------


class Resp:
    def __init__(self, status=200, content=b"", text="", url="", js=None):
        self.status_code, self.content, self.text, self.url, self._js = status, content, text, url, js

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def install_mocks(mode):
    import datetime as dtmod
    import time as timemod

    import mapbox_vector_tile
    import requests

    real_dt = dtmod.datetime

    class FrozenDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 25, 12, 0, 0, tzinfo=tz or dtmod.timezone.utc)

    dtmod.datetime = FrozenDT
    timemod.sleep = lambda s: print(f"[mock] sleep {s}")

    tile_count = {"n": 0}

    def sess_get(self, url, headers=None, timeout=None, **kw):
        print(f"[mock] GET {url}")
        if "/tiles/segments/" in url:
            if mode.get("tile401"):
                return Resp(401)
            n = tile_count["n"]
            tile_count["n"] += 1
            ids = {0: [101, 102, 103, 104, 108], 1: [104, 105, 106, 107, 109, 110]}.get(n, [])
            if mode.get("sem_107"):
                ids = [i for i in ids if i != 107]
            feats = [{"geometry": "POINT(10 10)", "properties": tile_props(i)} for i in ids]
            content = mapbox_vector_tile.encode([{"name": "segments", "features": feats}])
            return Resp(200, content=content, url=url)
        sid = int(url.rstrip("/").split("/")[-1])
        if mode.get("login"):
            return Resp(200, text="", url="https://www.strava.com/login")
        if mode.get("nonext"):
            return Resp(200, text="<html></html>", url=url)
        html = ('<html><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(next_data(sid, mode.get("leaderboard", {}).get(sid))) + "</script></html>")
        return Resp(200, text=html, url=url)

    def req_get(url, auth=None, params=None, timeout=None, **kw):
        print(f"[mock] GET {url} auth={auth} params={params}")
        if mode.get("curva_vazia"):
            return Resp(200, js={"list": []})
        c = json.loads(json.dumps(CURVA))
        if mode.get("sem_cs"):
            c["paceModels"] = []
        if mode.get("ruido_meio"):
            c["distance"] = [50] + c["distance"]
            c["values"] = [20] + c["values"]
            c["activity_id"] = ["i0"] + c["activity_id"]
        outro = {"type": "OUTRO", "criticalSpeed": 9.9, "dPrime": 9.9, "r2": 0.1}
        if mode.get("cs_segundo"):
            c["paceModels"] = [outro] + c["paceModels"]
        if mode.get("so_outro"):
            c["paceModels"] = [outro]
        return Resp(200, js={"list": [c]})

    requests.Session.get = sess_get
    requests.get = req_get
