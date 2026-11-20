"""
Clima vía Open-Meteo (sin API key).

- Histórico (para entrenar): archive-api.open-meteo.com (reanálisis ERA5), datos diarios.
- Previsión (para predecir): api.open-meteo.com/v1/forecast, datos diarios.

Variable clave para fotovoltaica: shortwave_radiation_sum (MJ/m² al día) = energía solar
que llega al plano horizontal. Es el mejor predictor de la generación diaria.
"""
import requests

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"

DAILY_VARS = [
    "shortwave_radiation_sum",        # MJ/m² -> principal predictor
    "temperature_2m_mean",
    "temperature_2m_max",
    "sunshine_duration",              # s
    "daylight_duration",              # s
    "precipitation_sum",              # mm
    "cloud_cover_mean",               # % (si disponible)
]


def _get_daily(url, lat, lon, params_extra):
    params = {
        "latitude": round(float(lat), 4),
        "longitude": round(float(lon), 4),
        "daily": ",".join(DAILY_VARS),
        "timezone": "auto",
    }
    params.update(params_extra)
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    d = r.json().get("daily", {})
    dates = d.get("time", [])
    rows = []
    for i, day in enumerate(dates):
        row = {"date": day}
        for v in DAILY_VARS:
            arr = d.get(v)
            row[v] = (arr[i] if arr and i < len(arr) else None)
        rows.append(row)
    return rows


def history_daily(lat, lon, start_date, end_date):
    """Clima diario histórico entre dos fechas 'YYYY-MM-DD' (inclusive)."""
    return _get_daily(ARCHIVE, lat, lon, {"start_date": start_date, "end_date": end_date})


def forecast_daily(lat, lon, forecast_days=3, past_days=0):
    """Clima diario previsto (y opcionalmente días pasados recientes)."""
    return _get_daily(FORECAST, lat, lon,
                      {"forecast_days": int(forecast_days), "past_days": int(past_days)})
