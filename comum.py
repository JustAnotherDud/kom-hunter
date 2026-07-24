# -*- coding: utf-8 -*-
"""comum.py — utilitários partilhados por explore.py e segment_detail.py.

Parsing de tempo e headers copiados/adaptados de club-koms/comum.py.
Acrescenta: tiles de segmentos (Mapbox Vector Tile), leitura do
__NEXT_DATA__ das páginas Strava, e um modelo GAP grosseiro (Minetti) para
o pré-filtro da Fase 1.

Achados do reconhecimento (24 Jul 2026, ver handoff):
- cdn-1.strava.com/tiles/segments/<athleteId>/<z>/<x>/<y> exige sessão
  autenticada (401 sem cookie, mesmo com athleteId diferente — não é só
  "security by obscurity" via ID na URL).
- properties.activityType == 9 confirmado como "Run" (spot-check contra
  metadata.activityType da página de detalhe).
- properties.komElapsedTime do tile bate certo com
  initialLeaderboard.leaderboard[0].elapsedTime da página de detalhe em
  todos os spot-checks — mas o detalhe é sempre a fonte de verdade (Fase 2),
  o tile só serve para o pré-filtro grosseiro (Fase 1).
"""
import json
import math
import re
import time
from datetime import date
from pathlib import Path

import mapbox_vector_tile
import requests

MESES = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
         "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"}

# mesma disciplina do club-koms/scrape.py — mas aqui os pedidos por
# segmento não são páginas que já visitarias na navegação normal, por isso
# convém não subir isto sem motivo.
PAGE_DELAY = 1.5

TILE_BASE = "https://cdn-1.strava.com/tiles/segments"
ACTIVITY_TYPE_RUN = 9


def iso_date(s):
    m = re.match(r"([A-Za-z]{3})\w* (\d+), (\d+)", s.strip())
    return (date(int(m.group(3)), MESES[m.group(1)[:3]], int(m.group(2))).isoformat()
            if m else s)


def parse_tempo(s):
    """'25s' / '2:29' / '1:20:16' -> segundos totais (int)."""
    s = s.strip()
    if s.endswith("s") and ":" not in s:
        return int(s[:-1])
    partes = [int(p) for p in s.split(":")]
    while len(partes) < 3:
        partes.insert(0, 0)
    h, m, sec = partes
    return h * 3600 + m * 60 + sec


def format_tempo(segundos):
    """segundos totais -> 'M:SS' ou 'H:MM:SS'."""
    h, resto = divmod(int(segundos), 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def normalizar_tempo(s):
    return format_tempo(parse_tempo(s))


def extrair_seg_id(url):
    m = re.search(r"/segments/(\d+)", url)
    return m.group(1) if m else None


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
    """Largura aproximada (km, na longitude) de um tile a este zoom/latitude.
    Serve só para reportar cobertura ao utilizador — ver explore.py. Medido
    empiricamente (25 Jul 2026): a densidade real de segmentos só estabiliza
    a partir de z15 (~19/km² em Rio Maior); a z10 media-se ~170x menos —
    não é bug de filtro, é decimação normal de tile piramidal."""
    n = 2 ** zoom
    return (360.0 / n) * 111.32 * math.cos(math.radians(lat))


INTENT_DEFAULT = "explore"  # NUNCA "popular" — testado (25 Jul 2026): "popular" filtra
# a menos de 2/3 dos segmentos devolvidos por qualquer outro valor ("explore", "browse",
# "nearby", "top", "recent" deram todos o mesmo resultado, maior) no mesmo tile/zoom.


def obter_tile_segmentos(sessao, athlete_id, zoom, x, y, intent=INTENT_DEFAULT):
    """Pede um tile de segmentos e devolve a lista de features (dicts).
    Termina o processo (SystemExit) se a sessão expirou."""
    url = (f"{TILE_BASE}/{athlete_id}/{zoom}/{x}/{y}"
           f"?intent={intent}&elevation_filter=all&surface_types=0&distance_min=0"
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


# --- modelo GAP grosseiro (Minetti et al. 2002) ---
# Custo energético relativo de correr a um dado grade, normalizado ao custo
# em plano. Serve só para o pré-filtro da Fase 1 (usa grade média, não o
# perfil de elevação completo) — a Fase 3 substitui isto por streams reais
# + curva de critical pace do Intervals.icu.

GRADE_MAX_PLAUSIVEL_PCT = 45.0  # fora disto só pode ser ruído de GPS/altímetro, não corrida real


def custo_minetti(grade_pct):
    # o polinómio de Minetti é de 5º grau — fora do intervalo em que foi
    # ajustado (grades reais de corrida), explode sem sentido físico. Um
    # passo de stream com pouca distância + ruído de elevação facilmente
    # implica grades de centenas de %; sem isto, um único ponto ruidoso
    # infla a distância efectiva de todo o segmento (visto em produção
    # num segmento de 14.8km/5571 pontos, ver kom_hunter/gap_model.py).
    grade_pct = max(-GRADE_MAX_PLAUSIVEL_PCT, min(GRADE_MAX_PLAUSIVEL_PCT, grade_pct))
    i = grade_pct / 100.0
    custo = 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6
    custo_plano = 3.6
    return max(custo, 0.5) / custo_plano


def distancia_efetiva(distancia_m, grade_pct):
    return distancia_m * custo_minetti(grade_pct)


def tempo_previsto_grosseiro(distancia_m, grade_pct, pace_flat_s_por_km):
    """Previsão grosseira (segundos), assumindo pace_flat_s_por_km fixo
    independente da duração — placeholder até a Fase 3 trazer a curva de
    critical pace real."""
    dist_efetiva_km = distancia_efetiva(distancia_m, grade_pct) / 1000.0
    return dist_efetiva_km * pace_flat_s_por_km
