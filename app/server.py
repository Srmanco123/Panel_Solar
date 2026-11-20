"""
Backend local (Flask) del panel solar. Sirve la SPA y una API que trae los datos
de Deye al vuelo por día / mes / año, con economía (€) usando precios configurables.

Arranca con:  python server.py   (o desde la app: preview "deye-panel")
Luego abre http://localhost:8733
"""
import os
import re
import json
import math
import time
import threading
import datetime as dt
from flask import Flask, jsonify, request, send_from_directory

import deye_client
import weather
import dataset
import model as modelmod
import battery
import prices

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DEYE_PORT", "8733"))
CONFIG_PATH = os.path.join(HERE, "config.json")
CSV_PATH = os.path.join(HERE, "data", "history.csv")

app = Flask(__name__, static_folder=None)

# ---- estado compartido (se inicializa perezosamente, una sola vez) ----
_S = {}
_S_LOCK = threading.Lock()


def S():
    if _S:
        return _S
    with _S_LOCK:
        if not _S:
            cfg = deye_client.load_config("config.json")
            cl = deye_client.DeyeClient(cfg)
            sid, lat, lon = cl.resolve_station(cfg.get("stationId"))
            bundle = None
            mp = os.path.join(HERE, "models", "model.joblib")
            if os.path.exists(mp):
                bundle = modelmod.load(mp)
            compra, exced = prices.build_models(cfg)
            _S.update(cfg=cfg, cl=cl, sid=sid, lat=lat, lon=lon, bundle=bundle,
                      compra=compra, exced=exced, inst=cfg["instalacion"])
    return _S


# ---- errores siempre en JSON (la web los muestra en vez de quedarse en "Cargando…") ----
class BadRequest(ValueError):
    pass


@app.errorhandler(BadRequest)
def _bad_request(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(Exception)
def _any_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    if isinstance(e, deye_client.DeyeError):
        return jsonify({"error": "DeyeCloud no responde o rechaza la petición: " + str(e)[:200]}), 502
    app.logger.exception("error interno")
    return jsonify({"error": f"error interno: {type(e).__name__}"}), 500


def _arg_date(name, default):
    v = request.args.get(name) or default
    try:
        return dt.date.fromisoformat(v).isoformat()
    except ValueError:
        raise BadRequest(f"fecha no válida: {v!r} (formato AAAA-MM-DD)")


# ---- caché en memoria: Deye y el clima son lentos, y el pasado no cambia ----
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    with _CACHE_LOCK:
        if len(_CACHE) > 400:
            for k in [k for k, v in _CACHE.items() if v[0] <= now]:
                _CACHE.pop(k, None)
        _CACHE[key] = (now + ttl, val)
    return val


def _hist(start, end, g):
    """station_history cacheado: 6 h si el periodo ya está cerrado, 3 min si incluye hoy."""
    s = S()
    today = dt.date.today()
    if g in (1, 2):
        closed = str(end) < today.isoformat()
    else:
        closed = int(str(end)[:4]) < today.year
    return _cached(("hist", str(start), str(end), g), 6 * 3600 if closed else 180,
                   lambda: s["cl"].station_history(s["sid"], start, end, g))


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _weather_rows(dates):
    """Clima diario para esas fechas. Histórico (reanálisis) y, para los días recientes que
    el histórico aún no trae, la API de previsión (que guarda ~3 meses hacia atrás)."""
    s = S()
    lo, hi = min(dates), max(dates)
    by = {}
    try:
        for w in _cached(("wx", lo, hi), 3 * 3600, lambda: weather.history_daily(s["lat"], s["lon"], lo, hi)):
            by[w["date"]] = w
    except Exception:
        pass
    today = dt.date.today()
    missing = [d for d in dates if by.get(d, {}).get("shortwave_radiation_sum") is None]
    recent = [d for d in missing if 0 <= (today - dt.date.fromisoformat(d)).days <= 90]
    if recent:
        past = min(92, (today - dt.date.fromisoformat(min(recent))).days + 1)
        try:
            for w in _cached(("fx", past), 1800,
                             lambda: weather.forecast_daily(s["lat"], s["lon"], forecast_days=2, past_days=past)):
                if w["date"] in recent and w.get("shortwave_radiation_sum") is not None:
                    by[w["date"]] = w
        except Exception:
            pass
    return by


def _estimates(dates):
    """{fecha: generación estimada (kWh)}.
    - Días usados para entrenar: predicción FUERA DE MUESTRA (validación por bloques), para que
      la comparación real vs estimado no esté inflada por días que el modelo ya 'se sabe'.
    - Resto de días: el modelo aplicado al clima real de ese día."""
    s = S()
    dates = sorted({d for d in dates if d})
    b = s["bundle"]
    if not b or not dates:
        return {}
    oos = {x["date"]: x["pred"] for x in b.get("backtest", [])}
    out = {d: round(float(oos[d]), 1) for d in dates if d in oos}
    rest = [d for d in dates if d not in out]
    if rest:
        wx = _weather_rows(rest)
        rows = [dataset.feature_row(wx[d]) for d in rest if d in wx]
        if rows:
            try:
                preds = modelmod.predict(b, rows)   # por lotes (rápido)
                for r, p in zip(rows, preds):
                    if p == p:                      # descarta NaN (día sin dato de radiación)
                        out[r["date"]] = round(max(0.0, float(p)), 1)
            except Exception:
                app.logger.exception("fallo al estimar")
    return out


def _first_data_date():
    """Primer día con datos del inversor (para no 'estimar' meses en que aún no existía)."""
    def load():
        try:
            import pandas as pd
            return dt.date.fromisoformat(str(pd.read_csv(CSV_PATH, usecols=["date"])["date"].min()))
        except Exception:
            return dt.date(2000, 1, 1)
    return _cached(("first",), 3600, load)


def _regime_note(start, end):
    """Aviso si el periodo incluye días de puesta en marcha (sin vertido a red)."""
    cutoff = (S()["bundle"] or {}).get("cutoff")
    first = _first_data_date().isoformat()
    if cutoff and start < cutoff and end >= first:
        d = dt.date.fromisoformat(cutoff) - dt.timedelta(days=1)
        return (f"Hasta el {d.day}/{d.month} la instalación no vertía a red (producción recortada al consumo). "
                "Ahí la estimación es lo que PODRÍA haber producido; esos días no cuentan para el acierto.")
    return None


def _acc_ok(date_iso):
    """¿Cuenta este día para medir el acierto? No los de puesta en marcha (sin vertido)."""
    cutoff = (S()["bundle"] or {}).get("cutoff")
    return not cutoff or date_iso >= cutoff


# ---------------- precios / economía ----------------
def _avg_price(pm, date_iso, weekend):
    """€/kWh medio de un día. Para precios reales de REE no pide días futuros."""
    if pm.mode in ("pvpc", "spot"):
        date_iso = min(date_iso, (dt.date.today() - dt.timedelta(days=1)).isoformat())
    return pm.avg(date_iso, weekend)


def _month_weekend_frac(y, m):
    n = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).day
    return sum(1 for d in range(1, n + 1) if dt.date(y, m, d).weekday() >= 5) / n


def _econ_rows(items, s):
    """Economía aproximada de un periodo. items = [(fila, fecha_ref, fracción_fin_de_semana)].
    Aproximada porque reparte la compra/vertido de cada día de forma uniforme entre las 24 h;
    el cálculo exacto por horas solo se hace en la vista de un día."""
    cost = income = sin_solar = kwh_p = kwh_e = 0.0
    for r, ref, wf in items:
        cp = wf * _avg_price(s["compra"], ref, True) + (1 - wf) * _avg_price(s["compra"], ref, False)
        ep = wf * _avg_price(s["exced"], ref, True) + (1 - wf) * _avg_price(s["exced"], ref, False)
        cost += r["purchase"] * cp
        income += r["export"] * ep
        sin_solar += r["cons"] * cp
        kwh_p += r["purchase"]
        kwh_e += r["export"]
    return {"cost": round(cost, 1), "income": round(income, 1), "net": round(income - cost, 1),
            "sin_solar": round(sin_solar, 1), "ahorro": round(sin_solar - cost + income, 1),
            "precio_compra": round(cost / kwh_p, 4) if kwh_p else None,
            "precio_excedente": round(income / kwh_e, 4) if kwh_e else None}


def _price_summary(s):
    today = dt.date.today().isoformat()
    return {
        "compra": {"modo": s["compra"].mode, "curva": s["compra"].curve(today, False),
                   "media": round(_avg_price(s["compra"], today, False), 4)},
        "excedente": {"modo": s["exced"].mode, "media": round(_avg_price(s["exced"], today, False), 4)},
    }


def _year_rows(y):
    rows = _agg_rows(_hist(f"{y}-01", f"{y}-12", 3), "month")
    return [r for r in rows if isinstance(r["label"], (int, float)) and 1 <= int(r["label"]) <= 12]


def _year_econ(y, rows, s):
    return _econ_rows([(r, f"{y}-{int(r['label']):02d}-15", _month_weekend_frac(int(y), int(r["label"])))
                       for r in rows], s)


# ---------------- API ----------------
@app.get("/api/overview")
def overview():
    s = S()
    cl, sid = s["cl"], s["sid"]
    try:
        st = _cached(("detail",), 120, lambda: cl.station_detail(sid))
    except Exception:
        st = {}
    year = dt.date.today().year
    try:
        rows = _year_rows(year)
    except Exception:
        rows = []
    tot = {k: sum(r[k] for r in rows) for k in ("gen", "cons", "export", "purchase")}
    econ = _year_econ(year, rows, s) if rows else {"income": 0, "cost": 0, "net": 0}
    met = {}
    b = s["bundle"]
    if b:
        mn = b["model_name"]
        met = {"r2": b["metrics"][mn]["r2"], "mae": b["metrics"][mn]["mae"], "model": mn,
               "validation": b.get("validation", "cv-aleatoria")}
    return jsonify({
        "station": {"name": st.get("name"), "kwp": st.get("installedCapacity"),
                    "lat": s["lat"], "lon": s["lon"], "soc": _num(st.get("batterySOC"))},
        "year": year,
        "year_totals": {"gen": tot["gen"], "cons": tot["cons"], "export": tot["export"], "purchase": tot["purchase"],
                        "self_suff": round((1 - tot["purchase"] / tot["cons"]) * 100) if tot["cons"] else None,
                        "income": econ["income"], "cost": econ["cost"], "net": econ["net"]},
        "prices": _price_summary(s),
        "model": met,
    })


def _day_hourly(intraday):
    """Integra la potencia intradía a kWh por hora: (compra_h, vertido_h).
    wirePower > 0 = compra de red; < 0 = vertido."""
    pts = sorted([r for r in intraday if r.get("timeStamp")], key=lambda r: int(r["timeStamp"]))
    imp, exp = {}, {}
    for i, r in enumerate(pts):
        ts = int(r["timeStamp"])
        dur = 300
        if i + 1 < len(pts):
            dur = min(900, max(0, int(pts[i + 1]["timeStamp"]) - ts))
        w = _num(r.get("wirePower"))
        if w is None:
            w = (_num(r.get("purchasePower")) or 0) - abs(_num(r.get("gridPower")) or 0)
        h = dt.datetime.fromtimestamp(ts).hour
        k = dur / 3600.0 / 1000.0
        imp[h] = imp.get(h, 0.0) + max(0.0, w) * k
        exp[h] = exp.get(h, 0.0) + max(0.0, -w) * k
    return imp, exp


@app.get("/api/day")
def day():
    s = S()
    date = _arg_date("date", dt.date.today().isoformat())
    weekend = prices.is_weekend(date)
    intraday = _hist(date, date, 1)
    series = []
    for r in sorted(intraday, key=lambda x: x.get("timeStamp") or 0):
        ts = r.get("timeStamp")
        if not ts:
            continue
        t = dt.datetime.fromtimestamp(int(ts))
        wire = _num(r.get("wirePower"))
        if wire is None:
            wire = (_num(r.get("purchasePower")) or 0) - abs(_num(r.get("gridPower")) or 0)
        bat = _num(r.get("batteryPower"))          # > 0 descarga, < 0 carga
        if bat is None:
            bat = abs(_num(r.get("dischargePower")) or 0) - abs(_num(r.get("chargePower")) or 0)
        series.append({"t": round(t.hour + t.minute / 60, 3),
                       "gen": (_num(r.get("generationPower")) or 0) / 1000.0,
                       "cons": (_num(r.get("consumptionPower")) or 0) / 1000.0,
                       "soc": _num(r.get("batterySOC")),
                       "purchase": max(0.0, wire) / 1000.0,
                       "export": max(0.0, -wire) / 1000.0,
                       "grid": wire / 1000.0,
                       "charge": max(0.0, -bat) / 1000.0,
                       "discharge": max(0.0, bat) / 1000.0,
                       "battery": bat / 1000.0})
    tot = {}
    try:
        d2 = _hist(date, date, 2)
        if d2:
            tot = d2[0]
    except Exception:
        pass
    gen_kwh = _num(tot.get("generationValue")) or 0
    cons_kwh = _num(tot.get("consumptionValue")) or 0
    if not tot and len(series) > 1:
        # hoy aún no hay total diario oficial: se integra la curva intradía
        def integ(k):
            return round(sum(a[k] * min(0.25, max(0.0, b["t"] - a["t"])) for a, b in zip(series, series[1:])), 1)
        gen_kwh, cons_kwh = integ("gen"), integ("cons")
    imp_h, exp_h = _day_hourly(intraday)
    imp_kwh, exp_kwh = sum(imp_h.values()), sum(exp_h.values())
    purchase_kwh = _num(tot.get("purchaseValue")) or round(imp_kwh, 2)
    export_kwh = _num(tot.get("gridValue")) or round(exp_kwh, 2)
    # € hora a hora (cada kWh a su precio), reescalado al contador oficial del día
    cost = sum(k * s["compra"].hour_price(date, h, weekend) for h, k in imp_h.items())
    income = sum(k * s["exced"].hour_price(date, h, weekend) for h, k in exp_h.items())
    if imp_kwh > 0.05:
        cost *= purchase_kwh / imp_kwh
    else:
        cost = purchase_kwh * _avg_price(s["compra"], date, weekend)
    if exp_kwh > 0.05:
        income *= export_kwh / exp_kwh
    else:
        income = export_kwh * _avg_price(s["exced"], date, weekend)
    sin_solar = cons_kwh * _avg_price(s["compra"], date, weekend)
    est = _estimates([date]).get(date)
    return jsonify({
        "date": date, "weekend": weekend, "series": series, "est": est,
        "price_curve": s["compra"].curve(date, weekend),
        "export_price_curve": s["exced"].curve(date, weekend),
        "totals": {"gen": gen_kwh, "cons": cons_kwh, "export": export_kwh, "purchase": purchase_kwh, "est": est},
        "economics": {"cost": round(cost, 2), "income": round(income, 2), "net": round(income - cost, 2),
                      "sin_solar": round(sin_solar, 2), "ahorro": round(sin_solar - cost + income, 2)},
    })


def _agg_rows(items, key):
    """Normaliza items de mes(g2: día) o año(g3: mes) a una serie uniforme."""
    out = []
    for it in items:
        label = it.get(key)
        out.append({
            "label": label,
            "gen": _num(it.get("generationValue")) or 0,
            "cons": _num(it.get("consumptionValue")) or 0,
            "export": _num(it.get("gridValue")) or 0,
            "purchase": _num(it.get("purchaseValue")) or 0,
            "charge": _num(it.get("chargeValue")) or 0,
            "discharge": _num(it.get("dischargeValue")) or 0,
        })
    out.sort(key=lambda r: r["label"] if isinstance(r["label"], (int, float)) else 0)
    return out


def _mae(pairs):
    return round(sum(pairs) / len(pairs), 2) if pairs else None


@app.get("/api/month")
def month():
    s = S()
    ym = request.args.get("ym") or dt.date.today().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", ym):
        raise BadRequest(f"mes no válido: {ym!r} (formato AAAA-MM)")
    y, m = int(ym[:4]), int(ym[5:7])
    last = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).day
    rows = [r for r in _agg_rows(_hist(f"{ym}-01", f"{ym}-{last:02d}", 2), "day")
            if isinstance(r["label"], (int, float)) and 1 <= int(r["label"]) <= last]
    for r in rows:
        r["date"] = f"{ym}-{int(r['label']):02d}"
    est = _estimates([r["date"] for r in rows])
    pairs = []
    for r in rows:
        r["est"] = est.get(r["date"])
        r["acc"] = _acc_ok(r["date"]) and r["date"] < dt.date.today().isoformat()   # hoy aún está incompleto
        if r["est"] is not None and r["gen"] and r["acc"]:
            pairs.append(abs(r["gen"] - r["est"]))
    econ = _econ_rows([(r, r["date"], 1.0 if prices.is_weekend(r["date"]) else 0.0) for r in rows], s)
    return jsonify({"ym": ym, "rows": rows, "economics": econ, "est_mae": _mae(pairs),
                    "note": _regime_note(f"{ym}-01", f"{ym}-{last:02d}")})


@app.get("/api/year")
def year():
    s = S()
    y = request.args.get("y") or str(dt.date.today().year)
    if not re.fullmatch(r"20\d\d", y):
        raise BadRequest(f"año no válido: {y!r}")
    rows = _year_rows(y)
    # estimación agregada por mes: solo se suman los días con dato real (comparación justa)
    yi = int(y)
    last = min(dt.date.today() - dt.timedelta(days=1), dt.date(yi, 12, 31))
    dates, d0 = [], max(dt.date(yi, 1, 1), _first_data_date())
    while d0 <= last:
        dates.append(d0.isoformat())
        d0 += dt.timedelta(days=1)
    mest = {}
    for d, v in _estimates(dates).items():
        mest[int(d[5:7])] = mest.get(int(d[5:7]), 0) + v
    for r in rows:
        r["est"] = round(mest[int(r["label"])], 1) if int(r["label"]) in mest else None
        r["acc"] = _acc_ok(f"{y}-{int(r['label']):02d}-01")     # mes completo en régimen normal
    return jsonify({"y": y, "rows": rows, "economics": _year_econ(y, rows, s),
                    "note": _regime_note(f"{y}-01-01", f"{y}-12-31")})


@app.get("/api/range")
def range_data():
    s = S()
    end = _arg_date("end", (dt.date.today() - dt.timedelta(days=1)).isoformat())
    start = _arg_date("start", (dt.date.fromisoformat(end) - dt.timedelta(days=44)).isoformat())
    if start > end or (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days > 800:
        raise BadRequest("rango no válido (máx. ~2 años)")
    hist = _cached(("range", start, end), 600,
                   lambda: dataset.fetch_daily_history(s["cl"], s["sid"], start, end, 2))
    rows = [{"date": h["date"], "gen": h.get("generation_kwh"), "cons": h.get("consumption_kwh"),
             "export": h.get("grid_kwh"), "purchase": h.get("purchase_kwh"),
             "charge": h.get("charge_kwh"), "discharge": h.get("discharge_kwh")} for h in hist]
    rows.sort(key=lambda r: r["date"])
    est = _estimates([r["date"] for r in rows])
    pairs, gs = [], []
    for r in rows:
        r["est"] = est.get(r["date"])
        if r["est"] is not None and r.get("gen") and _acc_ok(r["date"]):
            pairs.append(abs(r["gen"] - r["est"]))
            gs.append((r["gen"], r["est"]))
    import numpy as np
    corr = round(float(np.corrcoef([g for g, _ in gs], [e for _, e in gs])[0, 1]), 3) if len(gs) > 2 else None

    def tot(k):
        return round(sum(r.get(k) or 0 for r in rows), 1)
    return jsonify({"start": start, "end": end, "rows": rows, "est_mae": _mae(pairs), "corr": corr,
                    "totals": {"gen": tot("gen"), "cons": tot("cons"), "export": tot("export"), "purchase": tot("purchase")}})


def _cons_profile(s):
    """Consumo diario esperado (kWh): media reciente, separando laborable / fin de semana."""
    base = float(s["inst"].get("consumo_diario_kwh_estimado", 10))
    if not os.path.exists(CSV_PATH):
        return base, base
    import pandas as pd
    df = pd.read_csv(CSV_PATH)[["date", "consumption_kwh"]].dropna().tail(56)
    if df.empty:
        return base, base
    we = pd.to_datetime(df["date"]).dt.weekday >= 5
    allm = float(df["consumption_kwh"].mean())
    wd = float(df.loc[~we, "consumption_kwh"].mean()) if (~we).sum() >= 5 else allm
    wk = float(df.loc[we, "consumption_kwh"].mean()) if we.sum() >= 4 else allm
    return wd, wk


@app.get("/api/forecast")
def forecast():
    s = S()
    try:
        days = max(1, min(16, int(request.args.get("days", 7))))
    except ValueError:
        raise BadRequest("days debe ser un número")
    b = s["bundle"]
    if not b:
        return jsonify({"error": "sin modelo. Ejecuta: python cli.py train"}), 400
    fc = _cached(("fc", days), 1800, lambda: weather.forecast_daily(s["lat"], s["lon"], forecast_days=days))
    cons_wd, cons_we = _cons_profile(s)
    try:
        soc0 = _num(_cached(("latest",), 120, lambda: s["cl"].station_latest(s["sid"])).get("batterySOC")) \
            or s["inst"]["soc_reserva_valle"]
    except Exception:
        soc0 = s["inst"]["soc_reserva_valle"]
    rows = [dataset.feature_row(r) for r in fc]
    preds = modelmod.predict(b, rows) if rows else []
    out = []
    for i, (r, p) in enumerate(zip(rows, preds)):
        if p != p:      # sin radiación prevista para ese día
            continue
        gen = max(0.0, float(p))
        lo, hi = modelmod.band(b, gen)
        we = prices.is_weekend(r["date"])
        cons = cons_we if we else cons_wd
        # la recomendación usa los precios de la pestaña Precios (más barato / más caro del día)
        curve = s["compra"].curve(min(r["date"], dt.date.today().isoformat()), we)
        inst = dict(s["inst"], precio_valle_eur_kwh=min(curve), precio_punta_eur_kwh=max(curve))
        # prudente: decide la carga con el escenario bajo de la banda, no con el valor central
        rec = battery.decide_charge(lo, cons, soc0 if i == 0 else s["inst"]["soc_reserva_valle"], inst)
        out.append({"date": r["date"], "rad": r.get("shortwave_radiation_sum"),
                    "tmax": r.get("temperature_2m_max"), "gen": round(gen, 1),
                    "lo": round(lo, 1), "hi": round(hi, 1), "cons": round(cons, 1),
                    "status": rec["status"], "title": rec["title"], "target": rec["target_soc"],
                    "grid_kwh": rec["grid_charge_kwh"], "eur": rec["eur_valle"]})
    return jsonify({"forecast": out, "cons": round(cons_wd, 1),
                    "mae": b["metrics"][b["model_name"]]["mae"]})


@app.get("/api/live")
def live():
    s = S()
    d = _cached(("latest",), 60, lambda: s["cl"].station_latest(s["sid"]))
    return jsonify({k: _num(d.get(k)) for k in
                    ["generationPower", "consumptionPower", "gridPower", "purchasePower",
                     "batteryPower", "batterySOC", "chargePower", "dischargePower"]})


@app.get("/api/model")
def model_info():
    s = S()
    if not s["bundle"]:
        return jsonify({"error": "sin modelo. Ejecuta: python cli.py train"}), 400
    import pandas as pd
    import numpy as np
    b = s["bundle"]
    mn = b["model_name"]
    scatters = {}
    if os.path.exists(CSV_PATH):
        df = pd.read_csv(CSV_PATH)
        if b.get("cutoff"):
            df = df[df["date"] >= b["cutoff"]]
        vars_ = dict(dataset.WEATHER_VARS)
        if b.get("tilt") is not None:
            df = modelmod._add_poa(df, b["lat"], b["tilt"])
            vars_ = {"poa": f"Radiación en el plano a {b['tilt']:.0f}° (MJ/m²)", **vars_}
        for var, label in vars_.items():
            if var in df.columns:
                sub = df[[var, "generation_kwh"]].dropna()
                if len(sub) < 3:
                    continue
                pts = [{"x": round(float(a), 2), "y": round(float(g), 2)}
                       for a, g in zip(sub[var], sub["generation_kwh"])]
                r = float(np.corrcoef(sub[var], sub["generation_kwh"])[0, 1])
                scatters[var] = {"label": label, "points": pts, "r": None if math.isnan(r) else round(r, 2)}
    return jsonify({
        "metrics": {"r2": b["metrics"][mn]["r2"], "mae": b["metrics"][mn]["mae"],
                    "model": mn, "n": b["n_samples"]},
        "metrics_all": b["metrics"],
        "validation": b.get("validation", "cv-aleatoria"),
        "tilt": b.get("tilt"), "cutoff": b.get("cutoff"), "n_excluded": b.get("n_excluded", 0),
        "resid_q": b.get("resid_q"),
        "importance": b.get("importance", {}),
        "backtest": b.get("backtest", []),
        "scatters": scatters,
    })


# ---------------- precios (lectura / escritura) ----------------
def _default_precios(cfg):
    inst = cfg.get("instalacion", {})
    return {
        "compra": {"modo": "tramos", "fijo": 0.15, "tramos": [
            {"nombre": "valle", "horas": [[0, 8]], "findesemana": True, "precio": inst.get("precio_valle_eur_kwh", 0.10)},
            {"nombre": "llano", "horas": [[8, 10], [14, 18], [22, 24]], "precio": 0.15},
            {"nombre": "punta", "horas": [[10, 14], [18, 22]], "precio": inst.get("precio_punta_eur_kwh", 0.21)}]},
        "excedente": {"modo": "fijo", "fijo": inst.get("precio_excedente_eur_kwh", 0.06),
                      "tramos": [{"nombre": "todo el día", "horas": [[0, 24]], "precio": 0.06}]},
    }


def _clean_precios(data):
    """Valida y normaliza lo que llega de la web. Devuelve (precios, avisos)."""
    if not isinstance(data, dict):
        raise BadRequest("formato de precios no válido")
    out, warns = {}, []

    def price(v, what):
        try:
            p = float(v)
        except (TypeError, ValueError):
            raise BadRequest(f"{what}: precio no numérico")
        if not (0 <= p <= 2) or p != p:
            raise BadRequest(f"{what}: precio fuera de rango (0–2 €/kWh): {v}")
        return p
    for kind, modes, dflt in (("compra", ("fijo", "tramos", "pvpc"), 0.15),
                              ("excedente", ("fijo", "tramos", "spot"), 0.06)):
        sec = data.get(kind)
        if not isinstance(sec, dict) or sec.get("modo") not in modes:
            raise BadRequest(f"{kind}: modo no válido")
        tramos = []
        for i, t in enumerate(sec.get("tramos") or []):
            nombre = str(t.get("nombre") or f"tramo {i + 1}")[:30]
            horas = []
            for rng in t.get("horas") or []:
                try:
                    a, b = int(rng[0]), int(rng[1])
                except (TypeError, ValueError, IndexError):
                    raise BadRequest(f"{kind} · {nombre}: horas no válidas")
                if not (0 <= a < b <= 24):
                    raise BadRequest(f"{kind} · {nombre}: tramo {a}-{b} no válido (usa p. ej. 0-8)")
                horas.append([a, b])
            tramos.append({"nombre": nombre, "horas": horas, "precio": price(t.get("precio"), f"{kind} · {nombre}"),
                           "findesemana": bool(t.get("findesemana"))})
        fijo = price(sec.get("fijo", dflt), kind)
        if sec["modo"] == "tramos":
            if not tramos:
                raise BadRequest(f"{kind}: el modo por tramos necesita al menos un tramo")
            cover = [sum(1 for t in tramos for a, b in t["horas"] if a <= h < b) for h in range(24)]
            gaps = [h for h, c in enumerate(cover) if c == 0]
            dup = [h for h, c in enumerate(cover) if c > 1]
            if gaps:
                warns.append(f"{kind}: horas sin tramo {gaps} → se les aplica el precio fijo ({fijo} €)")
            if dup:
                warns.append(f"{kind}: horas en más de un tramo {dup} → manda el primero de la lista")
        out[kind] = {"modo": sec["modo"], "fijo": fijo, "tramos": tramos}
    return out, warns


@app.get("/api/prices")
def get_prices():
    cfg = deye_client.load_config("config.json")
    return jsonify(cfg.get("precios") or _default_precios(cfg))


@app.post("/api/prices")
def post_prices():
    # escribir la configuración solo desde este mismo PC (aunque el panel esté abierto a la WiFi)
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "los precios solo se pueden cambiar desde el propio PC"}), 403
    precios, warns = _clean_precios(request.get_json(force=True, silent=True))
    cfg = deye_client.load_config("config.json")
    cfg["precios"] = precios
    tmp = CONFIG_PATH + ".tmp"          # escritura atómica: nunca deja config.json a medias
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    s = S()
    s["cfg"] = cfg
    s["compra"], s["exced"] = prices.build_models(cfg)
    return jsonify({"ok": True, "prices": _price_summary(s), "warnings": warns})


# ---------------- SPA ----------------
@app.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "webapp"), "index.html")


@app.get("/<path:p>")
def static_files(p):
    return send_from_directory(os.path.join(HERE, "webapp"), p)


if __name__ == "__main__":
    host = os.environ.get("DEYE_HOST", "127.0.0.1")  # 0.0.0.0 para acceder desde el móvil (misma WiFi)
    print(f"Panel solar en  http://localhost:{PORT}  (host {host})")
    app.run(host=host, port=PORT, debug=False, threaded=True)
