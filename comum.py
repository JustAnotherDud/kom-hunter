# -*- coding: utf-8 -*-
"""comum.py: utilitários partilhados. Sessão Strava, tiles de segmentos
(Mapbox Vector Tile), __NEXT_DATA__ das páginas e custo de Minetti.

Os tiles exigem sessão autenticada (401 sem cookie ou com outro athleteId).
activityType == 9 é Run. O komElapsedTime do tile bate com o leaderboard da
página de detalhe, mas a fonte de verdade é o detalhe.
"""
import json
import math
import os
import re

import mapbox_vector_tile
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"}

# pausa (s) entre pedidos à Strava
PAGE_DELAY = 1.5

TILE_BASE = "https://cdn-1.strava.com/tiles/segments"
ACTIVITY_TYPE_RUN = 9


def fmt_pace(s_por_km):
    """206 -> '3:26'."""
    return f"{s_por_km // 60}:{s_por_km % 60:02d}"


def carregar_env(path=".env"):
    """Junta ao ambiente os pares CHAVE=valor de um .env simples. Uma variável
    já definida no ambiente ganha ao ficheiro."""
    if os.path.exists(path):
        for linha in open(path, encoding="utf-8"):
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                k, v = linha.split("=", 1)
                os.environ.setdefault(k, v)


def sessao_strava(cookie):
    """requests.Session com o cookie _strava4_session já definido."""
    s = requests.Session()
    s.cookies.set("_strava4_session", cookie, domain=".strava.com")
    return s


def deg2tile(lat, lon, zoom):
    """Coordenadas slippy-map (z/x/y) standard (OSM/Mapbox)."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tiles_no_raio(lat, lon, raio_km, zoom):
    """Lista (sem duplicados) de tiles (z, x, y) que cobrem o bounding box
    de raio_km à volta de (lat, lon)."""
    dlat = raio_km / 111.32
    dlon = raio_km / (111.32 * math.cos(math.radians(lat)) or 1e-9)
    x0, y0 = deg2tile(lat + dlat, lon - dlon, zoom)  # canto NW
    x1, y1 = deg2tile(lat - dlat, lon + dlon, zoom)  # canto SE
    return [(zoom, x, y)
            for x in range(min(x0, x1), max(x0, x1) + 1)
            for y in range(min(y0, y1), max(y0, y1) + 1)]


def largura_tile_km(lat, zoom):
    """Largura aproximada (km) de um tile a este zoom e latitude. Só para
    reportar cobertura."""
    n = 2 ** zoom
    return (360.0 / n) * 111.32 * math.cos(math.radians(lat))


INTENT_DEFAULT = "explore"  # "popular" devolve pelo menos 1/3 menos segmentos (testado)


def obter_tile_segmentos(sessao, athlete_id, zoom, x, y):
    """Pede um tile de segmentos e devolve a lista de features (dicts).
    Termina o processo (SystemExit) se a sessão expirou."""
    url = (f"{TILE_BASE}/{athlete_id}/{zoom}/{x}/{y}"
           f"?intent={INTENT_DEFAULT}&elevation_filter=all&surface_types=0&distance_min=0"
           "&creator=false&starred=false&top_10=false&overall=false&verified=false")
    r = sessao.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 401:
        raise SystemExit("Sessão expirada (401 no tile) — renovar STRAVA_SESSION.")
    r.raise_for_status()
    decoded = mapbox_vector_tile.decode(r.content)
    return decoded.get("segments", {}).get("features", [])


def obter_next_data(sessao, url):
    """GET a uma página Strava e devolve o __NEXT_DATA__ já parseado.
    Termina o processo (SystemExit) se a sessão expirou ou a página mudou."""
    r = sessao.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 401 or "/login" in r.url:
        raise SystemExit(f"Sessão expirada — renovar STRAVA_SESSION. ({url})")
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise SystemExit(f"__NEXT_DATA__ não encontrado — a página da Strava mudou? ({url})")
    return json.loads(m.group(1))


# --- custo de Minetti et al. (2002) ---
# Custo energético de correr a um dado grade, relativo ao plano. A Fase 1
# usa-o com a grade média, a Fase 3 passo a passo sobre os streams.

GRADE_MAX_PLAUSIVEL_PCT = 45.0  # fora disto é ruído de GPS/altímetro


def custo_minetti(grade_pct):
    # O polinómio de 5º grau explode fora das grades reais de corrida, e um
    # passo curto de stream com ruído de elevação chega a centenas de %.
    grade_pct = max(-GRADE_MAX_PLAUSIVEL_PCT, min(GRADE_MAX_PLAUSIVEL_PCT, grade_pct))
    i = grade_pct / 100.0
    custo = 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6
    custo_plano = 3.6
    return max(custo, 0.5) / custo_plano


def distancia_efetiva(distancia_m, grade_pct):
    return distancia_m * custo_minetti(grade_pct)


def tempo_previsto_grosseiro(distancia_m, grade_pct, pace_s_por_km):
    """Previsão grosseira (s) com um pace fixo. Só ordena a fila da Fase 1."""
    dist_efetiva_km = distancia_efetiva(distancia_m, grade_pct) / 1000.0
    return dist_efetiva_km * pace_s_por_km
