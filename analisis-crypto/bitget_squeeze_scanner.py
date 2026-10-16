#!/usr/bin/env python3
"""
Escáner de squeeze alcista en Bollinger Bands + MACD (velas SEMANALES)
para todos los futuros perpetuos USDT de Bitget.

Solo usa la librería estándar de Python (sin pandas ni ccxt) para poder
ejecutarse en cualquier sitio: tu PC, un servidor o GitHub Actions.

Uso:
    python3 bitget_squeeze_scanner.py                 # escanea y guarda resultados.json
    python3 bitget_squeeze_scanner.py --min-vol 5e6   # filtro de liquidez (USDT 24h)
    python3 bitget_squeeze_scanner.py --top 40

Indicadores (mismos parámetros que TradingView por defecto):
    Bollinger 20, 2σ (desviación poblacional)
    Keltner 20, 1.5 × rango verdadero medio (para el "TTM squeeze")
    MACD 12, 26, 9
"""

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
WEEK_MS = 7 * 24 * 3600 * 1000

BB_LEN, BB_MULT = 20, 2.0
KC_LEN, KC_MULT = 20, 1.5
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
RES_LOOKBACK = 26        # resistencia = máximo de las 26 semanas previas
SQZ_LOOKBACK = 8         # ventana para "hubo squeeze recientemente"
BW_RANK_WINDOW = 104     # percentil del ancho de banda sobre ~2 años
SQZ_POINT_WINDOW = 12    # semanas donde buscar el punto de máxima compresión
MIN_BARS = MACD_SLOW + MACD_SIG + 2

# Bitget lista también acciones, ETFs y materias primas como perpetuos USDT.
# El estudio es solo de cryptos, así que se excluyen.
NON_CRYPTO = {
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "AMZN", "GOOGL", "GOOG", "NFLX", "COIN",
    "MSTR", "PLTR", "ORCL", "AMD", "INTC", "HOOD", "CRCL", "AVGO", "MU", "TSM", "BABA",
    "ARM", "SMCI", "UBER", "SHOP", "PYPL", "DIS", "JPM", "GME", "AMC", "MARA", "RIOT",
    "CLSK", "IBIT", "SNOW", "ADBE", "CRM", "LLY", "UNH", "WMT", "COST", "BA", "XOM",
    "QQQ", "SPY", "TQQQ", "SQQQ", "IWM", "DIA", "EWY", "EWJ", "GLD", "SLV", "SOXL",
    "NVDL", "TSLL", "MSTU", "CONL", "XAU", "XAG", "XPT", "XPD", "COPPER", "CL", "NG",
    "BRENT", "WTI", "USO", "PAXG", "XAUT", "CIRCLE", "FIG", "BMNR", "SBET", "GEMI",
    "RDDT", "APP", "ASML", "QCOM", "TXN", "IBM", "CSCO", "V", "MA", "KO", "PEP", "NKE",
}


def is_non_crypto(ct):
    if ct.get("baseCoin", "").upper() in NON_CRYPTO:
        return True
    for k, v in ct.items():
        kl = k.lower()
        if ("rwa" in kl or "stock" in kl) and str(v).lower() in ("yes", "true", "1"):
            return True
    return False


# --------------------------------------------------------------------------
# API Bitget
# --------------------------------------------------------------------------
class ApiError(RuntimeError):
    """Error devuelto por Bitget (parámetros, símbolo...): no tiene sentido reintentar."""


def api_get(path, params, retries=4):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "squeeze-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:200]
            if e.code in (400, 404):
                raise ApiError(f"{path}: HTTP {e.code} {msg}") from None
            last_err = f"HTTP {e.code} {msg}"
        except Exception as e:  # noqa: BLE001
            last_err = e
        else:
            if body.get("code") != "00000":
                raise ApiError(f"{path}: {body.get('code')} {body.get('msg')}")
            return body["data"]
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Fallo en {url}: {last_err}")


def fetch_contracts():
    data = api_get("/api/v2/mix/market/contracts", {"productType": PRODUCT})
    return [d for d in data if d.get("symbolStatus") == "normal"]


def fetch_tickers():
    data = api_get("/api/v2/mix/market/tickers", {"productType": PRODUCT})
    return {d["symbol"]: d for d in data}


GRANULARITY = None       # se fija en la primera petición que funcione ("1Wutc" o "1W")
TARGET_BARS = 160        # ~3 años de velas semanales


def _add_rows(candles, rows):
    for r in rows or []:
        ts = int(r[0])
        candles[ts] = [ts] + [float(x) for x in r[1:7]]


def fetch_weekly(symbol):
    """Devuelve velas semanales ascendentes: [ts, o, h, l, c, vol_base, vol_usdt].

    /candles solo da el tramo reciente; el resto se pagina hacia atrás con
    /history-candles (máx. 200 por petición) usando endTime.
    """
    global GRANULARITY
    candles = {}
    grans = [GRANULARITY] if GRANULARITY else ["1Wutc", "1W"]
    for gran in grans:
        try:
            rows = api_get("/api/v2/mix/market/candles", {
                "symbol": symbol, "productType": PRODUCT, "granularity": gran, "limit": 1000,
            }, retries=2)
            GRANULARITY = gran
            _add_rows(candles, rows)
            break
        except RuntimeError:
            continue
    if not GRANULARITY:
        return []

    for _ in range(25):
        if len(candles) >= TARGET_BARS:
            break
        end = (min(candles) - 1) if candles else int(time.time() * 1000)
        try:
            rows = api_get("/api/v2/mix/market/history-candles", {
                "symbol": symbol, "productType": PRODUCT, "granularity": GRANULARITY,
                "endTime": end, "limit": 200,
            }, retries=2)
        except RuntimeError:
            # algunas granularidades exigen ventana startTime-endTime acotada
            try:
                rows = api_get("/api/v2/mix/market/history-candles", {
                    "symbol": symbol, "productType": PRODUCT, "granularity": GRANULARITY,
                    "startTime": end - 89 * 24 * 3600 * 1000, "endTime": end, "limit": 200,
                }, retries=2)
            except RuntimeError:
                break
        before = len(candles)
        _add_rows(candles, rows)
        if len(candles) == before:
            break
        time.sleep(0.06)
    return [candles[k] for k in sorted(candles)]


def probe(symbol="BTCUSDT"):
    """Diagnóstico: cuántas velas devuelve cada variante del API."""
    now = int(time.time() * 1000)
    tests = [
        ("candles 1Wutc", "/api/v2/mix/market/candles", {"granularity": "1Wutc", "limit": 1000}),
        ("candles 1W", "/api/v2/mix/market/candles", {"granularity": "1W", "limit": 1000}),
        ("candles 1W lim200", "/api/v2/mix/market/candles", {"granularity": "1W", "limit": 200}),
        ("history 1W", "/api/v2/mix/market/history-candles", {"granularity": "1W", "endTime": now, "limit": 200}),
        ("history 1Wutc", "/api/v2/mix/market/history-candles", {"granularity": "1Wutc", "endTime": now, "limit": 200}),
    ]
    for name, path, extra in tests:
        try:
            rows = api_get(path, {"symbol": symbol, "productType": PRODUCT, **extra}, retries=1)
            span = ""
            if rows:
                ts = sorted(int(r[0]) for r in rows)
                f = lambda t: datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d %a %H:%M")  # noqa: E731
                span = f"{f(ts[0])} → {f(ts[-1])}"
            print(f"  {name:<20} {len(rows or []):>4} velas  {span}", file=sys.stderr)
        except RuntimeError as e:
            print(f"  {name:<20} ERROR {e}", file=sys.stderr)
    full = fetch_weekly(symbol)
    print(f"  fetch_weekly({symbol}) = {len(full)} velas con {GRANULARITY}", file=sys.stderr)


# --------------------------------------------------------------------------
# Indicadores
# --------------------------------------------------------------------------
def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def stdev_pop(xs, n):
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    return out


def ema(xs, n):
    """EMA con semilla SMA; acepta None al principio de la serie."""
    out = [None] * len(xs)
    k = 2.0 / (n + 1)
    start = next((i for i, x in enumerate(xs) if x is not None), None)
    if start is None or len(xs) - start < n:
        return out
    seed_end = start + n - 1
    prev = sum(xs[start:seed_end + 1]) / n
    out[seed_end] = prev
    for i in range(seed_end + 1, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_range(h, l, c):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def pct_rank(window, value):
    """Percentil (0-100) de `value` dentro de `window` (0 = el más bajo)."""
    vals = [v for v in window if v is not None]
    if not vals:
        return None
    below = sum(1 for v in vals if v < value)
    return 100.0 * below / len(vals)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def lin(x, x0, x1, y0, y1):
    """Interpolación lineal acotada de x∈[x0,x1] a y∈[y0,y1]."""
    if x1 == x0:
        return y1
    t = clamp((x - x0) / (x1 - x0))
    return y0 + t * (y1 - y0)


# --------------------------------------------------------------------------
# Análisis de un símbolo
# --------------------------------------------------------------------------
def analyse(symbol, candles, ticker, now_ms):
    n = len(candles)
    if n < MIN_BARS:
        return None
    ts = [c[0] for c in candles]
    o = [c[1] for c in candles]
    h = [c[2] for c in candles]
    l = [c[3] for c in candles]
    c = [c[4] for c in candles]
    qv = [x[6] for x in candles]

    mid = sma(c, BB_LEN)
    sd = stdev_pop(c, BB_LEN)
    up = [m + BB_MULT * s if m is not None else None for m, s in zip(mid, sd)]
    lo = [m - BB_MULT * s if m is not None else None for m, s in zip(mid, sd)]
    bw = [(u - d) / m if m else None for u, d, m in zip(up, lo, mid)]
    pctb = [(x - d) / (u - d) if (u is not None and u != d) else None
            for x, u, d in zip(c, up, lo)]

    tr_ma = sma(true_range(h, l, c), KC_LEN)
    kc_up = [m + KC_MULT * t if (m is not None and t is not None) else None for m, t in zip(mid, tr_ma)]
    kc_lo = [m - KC_MULT * t if (m is not None and t is not None) else None for m, t in zip(mid, tr_ma)]
    sqz = [bool(u is not None and ku is not None and u < ku and d > kd)
           for u, d, ku, kd in zip(up, lo, kc_up, kc_lo)]

    ema_f, ema_s = ema(c, MACD_FAST), ema(c, MACD_SLOW)
    macd = [a - b if (a is not None and b is not None) else None for a, b in zip(ema_f, ema_s)]
    sig = ema(macd, MACD_SIG)
    hist = [m - s if (m is not None and s is not None) else None for m, s in zip(macd, sig)]

    i = n - 1          # vela en curso (la que ves en el gráfico)
    j = n - 2          # última vela semanal cerrada
    if None in (bw[i], hist[i], hist[j], hist[i - 2], mid[i - 3]):
        return None

    week_progress = clamp((now_ms - ts[i]) / WEEK_MS, 0.05, 1.0)

    # ---------------- Bollinger / squeeze ----------------
    bw_hist = bw[max(0, i - BW_RANK_WINDOW + 1):i + 1]
    bw_pct_now = pct_rank(bw_hist, bw[i])
    recent_bw = [b for b in bw[i - SQZ_LOOKBACK + 1:i + 1] if b is not None]
    bw_min_recent = min(recent_bw)
    bw_min_pct = pct_rank(bw_hist, bw_min_recent)
    bw_min_6m = min(b for b in bw[max(0, i - 25):i + 1] if b is not None)
    bw_6m_low = bw_min_recent <= bw_min_6m * 1.05

    sqz_now = sqz[i]
    sqz_recent = any(sqz[i - SQZ_LOOKBACK + 1:i + 1])
    sqz_fired = sqz_recent and not sqz_now
    # duración del squeeze más reciente dentro de la ventana
    sqz_weeks = 0
    k = i
    while k > i - SQZ_LOOKBACK and not sqz[k]:
        k -= 1
    while k >= 0 and sqz[k]:
        sqz_weeks += 1
        k -= 1

    # punto de máxima compresión reciente: desde ahí se mide cuánto ha corrido ya
    k_sq = min(range(i - SQZ_POINT_WINDOW + 1, i + 1), key=lambda q: bw[q] if bw[q] is not None else 1e9)
    gain_since_sqz = c[i] / c[k_sq] - 1
    peak_since_sqz = max(h[k_sq:i + 1])
    drawdown_from_peak = c[i] / peak_since_sqz - 1

    bw_expanding = bw[i] > bw[j] and bw[i] > bw_min_recent * 1.10
    above_mid = c[i] > mid[i]
    mid_slope = (mid[i] - mid[i - 3]) / mid[i - 3]
    above_upper_now = c[i] > up[i]
    above_upper_closed = c[j] > up[j]

    # ---------------- Resistencia ----------------
    res26 = max(h[max(0, i - RES_LOOKBACK):i])           # sin la vela actual
    res52 = max(h[max(0, i - 52):i])
    dist_res = c[i] / res26 - 1                           # >0 = por encima
    broke_res = c[i] > res26
    broke_res_closed = c[j] > max(h[max(0, j - RES_LOOKBACK):j])

    # ---------------- MACD ----------------
    macd_bull = macd[i] > sig[i]
    hist_pos = hist[i] > 0
    hist_rising = hist[i] > hist[j]
    rising_streak = 0
    k = i
    while k > 0 and hist[k] is not None and hist[k - 1] is not None and hist[k] > hist[k - 1]:
        rising_streak += 1
        k -= 1
    macd_rising = macd[i] > macd[j]
    macd_above_zero = macd[i] > 0
    cross_weeks = None
    for back in range(0, 6):
        a, b = i - back, i - back - 1
        if hist[b] is not None and hist[a] > 0 >= hist[b]:
            cross_weeks = back
            break
    zero_cross_recent = any(macd[k] is not None and macd[k - 1] is not None and macd[k] > 0 >= macd[k - 1]
                            for k in range(i - 3, i + 1))
    # MACD normalizado por precio para comparar entre monedas
    macd_norm = macd[i] / c[i] * 100

    # ---------------- Precio / volumen ----------------
    rng = h[i] - l[i]
    close_pos = (c[i] - l[i]) / rng if rng > 0 else 0.5
    green = c[i] > o[i]
    higher_lows = min(l[i - 3:i + 1]) > min(l[i - 7:i - 3])
    vol_avg = sum(qv[i - 20:i]) / 20 if i >= 20 else None
    vol_ratio_closed = qv[j] / vol_avg if vol_avg else None
    # extrapolar la semana en curso solo cuando ya lleva ≥ 3 días (si no, es ruido)
    vol_ratio_live = (qv[i] / week_progress) / vol_avg if (vol_avg and week_progress >= 0.4) else None
    vol_ratio = max(v for v in (vol_ratio_closed, vol_ratio_live, 0) if v is not None)
    ext_mid = c[i] / mid[i] - 1
    chg_week = c[i] / c[j] - 1
    chg_4w = c[i] / c[i - 4] - 1

    # ======================================================================
    # PUNTUACIÓN BREAKOUT (0-100): ¿cuánto se ha comprimido y qué tan cerca
    # está de romper (o acaba de romper) al alza?
    # ======================================================================
    b_parts = {}
    b_parts["compresion"] = lin(bw_min_pct, 50, 5, 0, 25) + (5 if bw_6m_low else 0)
    if sqz_now or sqz_fired:
        b_parts["squeeze_ttm"] = 10 + min(sqz_weeks, 5)
    elif sqz_recent:
        b_parts["squeeze_ttm"] = 6
    else:
        b_parts["squeeze_ttm"] = 0
    pb = pctb[i]
    b_parts["posicion_bandas"] = 20 if pb >= 1 else lin(pb, 0.5, 1.0, 0, 18)
    if broke_res:
        b_parts["resistencia"] = 20
    else:
        b_parts["resistencia"] = lin(-dist_res, 0.20, 0.0, 0, 16)
    b_parts["expansion_alcista"] = 10 if (bw_expanding and above_mid) else (4 if above_mid else 0)
    b_parts["volumen"] = lin(vol_ratio, 0.8, 2.0, 0, 5)
    # si ya corrió mucho desde el squeeze, la ruptura ya pasó: queda menos recorrido
    b_parts["ya_disparada"] = lin(gain_since_sqz, 0.5, 1.5, 0, -25)
    breakout = clamp(sum(b_parts.values()), 0, 100)

    # ======================================================================
    # PUNTUACIÓN CONTINUACIÓN (0-100): ¿el impulso alcista tiene gasolina?
    # ======================================================================
    c_parts = {}
    c_parts["macd_sobre_senal"] = 15 if macd_bull else 0
    c_parts["histograma"] = (8 if hist_pos else 0) + min(rising_streak, 3) * 4
    c_parts["macd_pendiente"] = 8 if macd_rising else 0
    if macd_above_zero:
        c_parts["macd_cero"] = 10 if not zero_cross_recent else 12
    else:
        c_parts["macd_cero"] = lin(macd_norm, -8, 0, 0, 5) if macd_rising else 0
    c_parts["cruce_reciente"] = 6 if (cross_weeks is not None and cross_weeks <= 3) else 0
    c_parts["precio_sobre_media"] = 10 if above_mid else 0
    c_parts["media_pendiente"] = lin(mid_slope, -0.02, 0.05, 0, 10)
    c_parts["minimos_crecientes"] = 5 if higher_lows else 0
    c_parts["vela"] = (lin(close_pos, 0.4, 0.9, 0, 6) + (2 if green else 0))
    c_parts["volumen"] = lin(vol_ratio, 1.0, 2.0, 0, 4)
    penalty = 0
    if pb > 1.35 or ext_mid > 0.45:
        penalty = -12          # sobreextendido: alta probabilidad de retroceso
    elif pb > 1.15 or ext_mid > 0.30:
        penalty = -5
    c_parts["sobreextension"] = penalty
    # mecha de rechazo: subió fuerte y devolvió buena parte
    c_parts["rechazo_desde_max"] = lin(drawdown_from_peak, -0.15, -0.45, 0, -10)
    continuation = clamp(sum(c_parts.values()), 0, 100)

    total = round(0.5 * breakout + 0.5 * continuation, 1)

    # ---------------- Estado legible ----------------
    if gain_since_sqz >= 0.9:
        estado = f"Ya disparada (+{gain_since_sqz * 100:.0f}% desde el squeeze)"
    elif (above_upper_closed or broke_res_closed) and sqz_recent and macd_bull:
        estado = "Ruptura confirmada (cierre semanal)"
    elif (above_upper_now or broke_res) and (sqz_recent or bw_min_pct <= 25) and macd_bull:
        estado = "Rompiendo ahora (falta cierre semanal)"
    elif (sqz_now or bw_min_pct <= 20) and pb >= 0.6 and macd_rising:
        estado = "Pre-ruptura (squeeze cargado)"
    elif sqz_now or bw_min_pct <= 20:
        estado = "Compresión (dirección sin definir)"
    elif above_mid and macd_bull:
        estado = "Tendencia alcista"
    else:
        estado = "Neutral / bajista"

    in_squeeze_context = sqz_recent or bw_min_pct <= 35

    tail = slice(max(0, n - 60), n)
    r = lambda v, d=8: None if v is None else round(v, d)  # noqa: E731
    return {
        "symbol": symbol,
        "coin": symbol.replace("USDT", ""),
        "price": c[i],
        "vol24h_usdt": float(ticker.get("usdtVolume") or ticker.get("quoteVolume") or 0),
        "funding": float(ticker.get("fundingRate") or 0),
        "weeks_history": n,
        "score_total": total,
        "score_breakout": round(breakout, 1),
        "score_continuacion": round(continuation, 1),
        "estado": estado,
        "squeeze_context": in_squeeze_context,
        "metrics": {
            "bandwidth_pct": round(bw[i] * 100, 2),
            "bandwidth_percentil": round(bw_pct_now, 1),
            "bandwidth_min_8s_percentil": round(bw_min_pct, 1),
            "bandwidth_min_6m": bw_6m_low,
            "squeeze_ttm_activo": sqz_now,
            "squeeze_ttm_disparado": sqz_fired,
            "semanas_squeeze": sqz_weeks,
            "bandas_expandiendo": bw_expanding,
            "pct_b": round(pb, 3),
            "precio_sobre_media20": above_mid,
            "pendiente_media20_3s": round(mid_slope * 100, 2),
            "sobre_banda_sup_ahora": above_upper_now,
            "sobre_banda_sup_cierre": above_upper_closed,
            "resistencia_26s": res26,
            "resistencia_52s": res52,
            "dist_resistencia_pct": round(dist_res * 100, 2),
            "rompe_resistencia": broke_res,
            "macd": r(macd[i]),
            "senal": r(sig[i]),
            "histograma": r(hist[i]),
            "macd_sobre_senal": macd_bull,
            "macd_sobre_cero": macd_above_zero,
            "histograma_subiendo_semanas": rising_streak,
            "cruce_alcista_hace_semanas": cross_weeks,
            "vol_ratio": round(vol_ratio, 2),
            "cambio_semana_pct": round(chg_week * 100, 2),
            "cambio_4s_pct": round(chg_4w * 100, 2),
            "ext_sobre_media_pct": round(ext_mid * 100, 2),
            "progreso_semana": round(week_progress, 2),
            "semanas_desde_compresion": i - k_sq,
            "subida_desde_squeeze_pct": round(gain_since_sqz * 100, 1),
            "retroceso_desde_max_pct": round(drawdown_from_peak * 100, 1),
            "max_desde_squeeze": peak_since_sqz,
        },
        "parts_breakout": {k: round(v, 1) for k, v in b_parts.items()},
        "parts_continuacion": {k: round(v, 1) for k, v in c_parts.items()},
        "series": {
            "t": ts[tail], "o": o[tail], "h": h[tail], "l": l[tail], "c": c[tail],
            "up": [r(x) for x in up[tail]], "mid": [r(x) for x in mid[tail]],
            "lo": [r(x) for x in lo[tail]], "macd": [r(x) for x in macd[tail]],
            "sig": [r(x) for x in sig[tail]], "hist": [r(x) for x in hist[tail]],
            "sqz": sqz[tail],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-vol", type=float, default=2e6, help="volumen 24h mínimo en USDT")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--out", default="resultados.json")
    ap.add_argument("--sleep", type=float, default=0.07, help="pausa entre peticiones (s)")
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    contracts = fetch_contracts()
    tickers = fetch_tickers()
    print(f"{len(contracts)} contratos USDT-M activos en Bitget", file=sys.stderr)

    print("Diagnóstico API (BTCUSDT):", file=sys.stderr)
    probe("BTCUSDT")

    for ct in contracts:
        if ct["symbol"] in ("AAPLUSDT", "BTCUSDT"):
            print(f"  contrato {ct['symbol']}: {json.dumps(ct, ensure_ascii=False)[:900]}", file=sys.stderr)

    results, skipped, short, errors = [], 0, [], 0
    excluded = [ct["symbol"] for ct in contracts if is_non_crypto(ct)]
    contracts = [ct for ct in contracts if not is_non_crypto(ct)]
    print(f"Excluidos {len(excluded)} no-crypto: {', '.join(excluded)}", file=sys.stderr)
    for idx, ct in enumerate(contracts, 1):
        sym = ct["symbol"]
        tk = tickers.get(sym, {})
        vol = float(tk.get("usdtVolume") or tk.get("quoteVolume") or 0)
        if vol < args.min_vol:
            skipped += 1
            continue
        try:
            candles = fetch_weekly(sym)
            res = analyse(sym, candles, tk, now_ms)
            if res:
                results.append(res)
            else:
                short.append((sym, len(candles)))
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  {sym}: {e}", file=sys.stderr)
        if idx % 50 == 0:
            print(f"  {idx}/{len(contracts)}", file=sys.stderr)
        time.sleep(args.sleep)

    results.sort(key=lambda x: x["score_total"], reverse=True)
    ranked = [x for x in results if x["squeeze_context"]]
    out = {
        "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "exchange": "Bitget USDT-M perpetuos",
        "temporalidad": "1W",
        "parametros": {
            "bollinger": [BB_LEN, BB_MULT], "keltner": [KC_LEN, KC_MULT],
            "macd": [MACD_FAST, MACD_SLOW, MACD_SIG], "min_vol_24h_usdt": args.min_vol,
        },
        "total_contratos": len(contracts) + len(excluded),
        "descartados_liquidez": skipped,
        "excluidos_no_crypto": len(excluded),
        "analizados": len(results),
        "historial_insuficiente": len(short),
        "errores": errors,
        "con_contexto_squeeze": len(ranked),
        "ranking": ranked[:args.top],
        "resto": [{k: x[k] for k in ("symbol", "score_total", "score_breakout",
                                     "score_continuacion", "estado")} for x in results
                  if not x["squeeze_context"]],
    }
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n{'#':>3} {'MONEDA':<12}{'TOTAL':>6}{'BRK':>6}{'CONT':>6}  %B    BW%ile  MACD  ESTADO")
    for n_, x in enumerate(ranked[:args.top], 1):
        m = x["metrics"]
        print(f"{n_:>3} {x['coin']:<12}{x['score_total']:>6}{x['score_breakout']:>6}"
              f"{x['score_continuacion']:>6}  {m['pct_b']:<5} {m['bandwidth_min_8s_percentil']:>6}"
              f"  {'↑' if m['macd_sobre_senal'] else '↓'}{'+' if m['macd_sobre_cero'] else '-'}   {x['estado']}")
    print(f"\nAnalizados {len(results)} · historial insuficiente {len(short)} · errores {errors}", file=sys.stderr)
    if short:
        print("  ej. sin historial suficiente: " + ", ".join(f"{a}({b})" for a, b in short[:15]), file=sys.stderr)
    print(f"Guardado en {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
