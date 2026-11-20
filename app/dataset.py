"""
Construye la tabla de entrenamiento uniendo:
  - histórico de generación/consumo del inversor Deye (station/history, por día)
  - clima diario histórico de Open-Meteo (misma lat/lon y fechas)
emparejados por fecha.
"""
import datetime as dt
import pandas as pd

import weather

# Campos de energía (kWh) que trae StationDataItem
ENERGY_FIELDS = {
    "generationValue": "generation_kwh",
    "consumptionValue": "consumption_kwh",
    "gridValue": "grid_kwh",
    "purchaseValue": "purchase_kwh",
    "chargeValue": "charge_kwh",
    "dischargeValue": "discharge_kwh",
}

# Variables meteo que usamos como predictores
FEATURES = [
    "shortwave_radiation_sum",
    "temperature_2m_mean",
    "temperature_2m_max",
    "sunshine_duration",
    "daylight_duration",
    "cloud_cover_mean",
    "precipitation_sum",
    "rad2",            # radiación² (capta la saturación del inversor)
    "clearness",       # radiación / horas de luz (índice de claridad)
    "temp_sat",        # exceso de Tª sobre 25 °C (pérdidas por calor)
    "doy_sin", "doy_cos",   # estacionalidad (día del año)
]

# Variables meteo "crudas" para las regresiones por variable (nombre legible)
WEATHER_VARS = {
    "shortwave_radiation_sum": "Radiación (MJ/m²)",
    "cloud_cover_mean": "Nubosidad (%)",
    "temperature_2m_max": "Tª máx (°C)",
    "sunshine_duration": "Horas de sol (s)",
    "precipitation_sum": "Precipitación (mm)",
}
TARGET = "generation_kwh"


def _item_date(item):
    """Extrae una fecha 'YYYY-MM-DD' de un StationDataItem, tolerante al formato."""
    y, m, d = item.get("year"), item.get("month"), item.get("day")
    if y and m and d:
        try:
            return dt.date(int(y), int(m), int(d)).isoformat()
        except ValueError:
            pass
    ts = item.get("timeStamp") or item.get("dateTime")
    if ts:
        s = str(ts)
        if s.isdigit() and len(s) >= 10:   # epoch segundos
            return dt.datetime.utcfromtimestamp(int(s[:10])).date().isoformat()
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return dt.datetime.strptime(s[:19], fmt).date().isoformat()
            except ValueError:
                continue
    return None


def normalize_history(items):
    """Lista de StationDataItem -> lista de dicts {date, generation_kwh, ...} (uno por día)."""
    out = {}
    for it in items:
        date = _item_date(it)
        if not date:
            continue
        row = {"date": date}
        for src, dst in ENERGY_FIELDS.items():
            v = it.get(src)
            if v is not None:
                row[dst] = float(v)
        # solo nos quedamos con días que tienen generación medida
        if "generation_kwh" in row:
            out[date] = row
    return list(out.values())


def fetch_daily_history(client, station_id, start_date, end_date, granularity=1, chunk_months=1):
    """Descarga el histórico diario en tramos (mes a mes por defecto) para no pedir rangos enormes."""
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    all_items = []
    cur = start
    while cur <= end:
        # fin del tramo
        nxt = cur
        for _ in range(chunk_months):
            # saltar al primer día del mes siguiente
            if nxt.month == 12:
                nxt = dt.date(nxt.year + 1, 1, 1)
            else:
                nxt = dt.date(nxt.year, nxt.month + 1, 1)
        chunk_end = min(end, nxt - dt.timedelta(days=1))
        items = client.station_history(station_id, cur.isoformat(), chunk_end.isoformat(), granularity)
        all_items.extend(items)
        cur = chunk_end + dt.timedelta(days=1)
    return normalize_history(all_items)


def _doy_features(date_iso):
    d = dt.date.fromisoformat(date_iso)
    doy = d.timetuple().tm_yday
    import math
    ang = 2 * math.pi * doy / 365.25
    return math.sin(ang), math.cos(ang)


def _isnum(v):
    return v is not None and v == v      # ni None ni NaN


def poa_daily(H, date_iso, lat, tilt):
    """Radiación diaria sobre el plano inclinado (MJ/m²) a partir de la horizontal H.
    Transposición clásica Liu-Jordan (cielo isótropo) + fracción difusa de Erbs, para un
    plano mirando al ecuador. Es lo que hace que el modelo extrapole bien a otra época
    del año: la misma radiación horizontal rinde mucho más en invierno sobre un panel inclinado."""
    import math
    if not _isnum(H) or lat is None or tilt is None:
        return float("nan")
    doy = dt.date.fromisoformat(str(date_iso)[:10]).timetuple().tm_yday
    phi, b = math.radians(abs(float(lat))), math.radians(float(tilt))
    d = math.radians(23.45) * math.sin(2 * math.pi * (284 + doy) / 365)
    if float(lat) < 0:
        d = -d
    clamp = lambda x: max(-1.0, min(1.0, x))
    ws = math.acos(clamp(-math.tan(phi) * math.tan(d)))
    e0 = 1 + 0.033 * math.cos(2 * math.pi * doy / 365)
    hor = math.cos(phi) * math.cos(d) * math.sin(ws) + ws * math.sin(phi) * math.sin(d)
    if hor <= 1e-6:
        return float("nan")
    h0 = (86400 / math.pi) * 1367 * e0 * hor / 1e6
    kt = max(0.0, min(1.0, H / h0))
    if math.degrees(ws) <= 81.4:
        fd = (1 - 0.2727 * kt + 2.4495 * kt**2 - 11.9514 * kt**3 + 9.3879 * kt**4) if kt < 0.715 else 0.143
    else:
        fd = (1 + 0.2832 * kt - 2.5557 * kt**2 + 0.8448 * kt**3) if kt < 0.722 else 0.175
    wsp = min(ws, math.acos(clamp(-math.tan(phi - b) * math.tan(d))))
    rb = (math.cos(phi - b) * math.cos(d) * math.sin(wsp) + wsp * math.sin(phi - b) * math.sin(d)) / hor
    hd = fd * H
    return (H - hd) * rb + hd * (1 + math.cos(b)) / 2 + H * 0.2 * (1 - math.cos(b)) / 2


def feature_row(w, lat=None, tilt=None):
    """Añade a una fila de clima las variables derivadas + estacionalidad.
    Se usa igual en entrenamiento y en predicción para que coincidan.
    Si falta la radiación, las derivadas quedan en NaN (no en 0: un 0 sería un día 'negro' falso)."""
    r = dict(w)
    a, b = _doy_features(r["date"])
    r["doy_sin"], r["doy_cos"] = a, b
    rad = r.get("shortwave_radiation_sum")
    dl = r.get("daylight_duration")
    tmax = r.get("temperature_2m_max")
    nan = float("nan")
    r["rad2"] = rad * rad if _isnum(rad) else nan
    r["clearness"] = rad / (dl / 3600.0 + 0.1) if _isnum(rad) and _isnum(dl) and dl else nan
    r["temp_sat"] = max(0.0, (tmax - 25.0)) if _isnum(tmax) else 0.0
    if lat is not None and tilt is not None:
        r["poa"] = poa_daily(rad, r["date"], lat, tilt)
        r["poa2"] = r["poa"] ** 2
    return r


def commissioning_cutoff(df):
    """Fecha del primer día con vertido a red. Antes de eso la instalación trabajaba sin
    exportar (producción recortada al consumo), y esos días engañan al modelo.
    Devuelve None si no aplica (sin columna, sin tramo inicial a 0, o si sería >50% de los datos)."""
    if "grid_kwh" not in df.columns:
        return None
    d = df.sort_values("date")
    exp = d["grid_kwh"].fillna(0).values > 0.05
    if not exp.any() or exp[0]:
        return None
    first = int(exp.argmax())
    if first > len(d) * 0.5:
        return None
    return str(d["date"].iloc[first])


def build_frame(daily_history, weather_rows):
    """Une generación (Deye) + clima (Open-Meteo) por fecha -> DataFrame listo para el modelo."""
    gen = pd.DataFrame(daily_history)
    wx = pd.DataFrame(weather_rows)
    if gen.empty:
        raise ValueError("No hay histórico de generación del Deye para esas fechas.")
    if wx.empty:
        raise ValueError("No hay datos de clima para esas fechas.")
    df = pd.merge(gen, wx, on="date", how="inner").sort_values("date").reset_index(drop=True)
    # variables derivadas + estacionalidad (mismas que en predicción)
    eng = pd.DataFrame([feature_row(r) for r in df.to_dict("records")])
    for col in ["doy_sin", "doy_cos", "rad2", "clearness", "temp_sat"]:
        df[col] = eng[col].values
    for col in FEATURES:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def usable_features(df):
    """Devuelve las features con datos suficientes (no todo NaN)."""
    return [c for c in FEATURES if c in df.columns and df[c].notna().sum() >= max(5, len(df) * 0.5)]
