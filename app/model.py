"""
Modelo de regresión multivariable: predice la generación diaria (kWh) a partir del clima.

Candidatos:
  - fisico  : regresión lineal sobre la radiación en el plano inclinado (POA) + POA² + calor.
              La inclinación efectiva se AJUSTA a los datos. Extrapola bien a otra época del año.
  - ridge   : lineal regularizado sobre todas las variables.
  - gboost / hgb / rforest : no lineales (árboles). Buenos interpolando, malos fuera de lo ya visto.

Validación HONESTA para series temporales (los días vecinos se parecen, así que barajar días
al azar infla el acierto):
  - walk-forward : entreno con el pasado y predigo la semana siguiente, avanzando. Es lo que
                   de verdad hace el modelo cuando predice mañana. SE ELIGE EL MODELO POR AQUÍ.
  - bloques      : validación cruzada por semanas completas (da una predicción fuera de muestra
                   para todos los días; se usa en las gráficas real vs estimado).

Guarda un "bundle" (modelo + features + métricas) con joblib.
"""
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor, HistGradientBoostingRegressor
from sklearn.model_selection import cross_val_predict, GroupKFold
from sklearn.metrics import r2_score, mean_absolute_error
import joblib

import dataset

PHYS_FEATURES = ["poa", "poa2", "temp_sat"]
WF_INIT, WF_STEP = 60, 7      # walk-forward: días iniciales de entrenamiento y tamaño del paso


def _linear(alpha=1.0):
    return Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler()),
                     ("m", Ridge(alpha=alpha))])


def _candidates(n_samples, features):
    """{nombre: (pipeline, lista de features)}"""
    cands = {"ridge": (_linear(), features)}
    if "poa" in features:
        cands["fisico"] = (_linear(0.1), [f for f in PHYS_FEATURES if f in features])
    if n_samples >= 40:   # los no lineales necesitan algo más de datos
        cands["gboost"] = (Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("m", GradientBoostingRegressor(n_estimators=250, max_depth=2, learning_rate=0.05,
                                            subsample=0.9, random_state=0))]), features)
        cands["hgb"] = (Pipeline([
            ("m", HistGradientBoostingRegressor(max_iter=400, max_depth=3, learning_rate=0.05,
                                                l2_regularization=1.0, random_state=0))]), features)
        cands["rforest"] = (Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("m", RandomForestRegressor(n_estimators=300, max_depth=9, min_samples_leaf=2,
                                        random_state=0, n_jobs=-1))]), features)
    return cands


def _walk_forward(pipe, X, y, init=WF_INIT, step=WF_STEP):
    """Predicciones 'como en la vida real': solo con datos anteriores a cada bloque."""
    n = len(y)
    init = min(init, max(8, n // 2))
    pred = np.full(n, np.nan)
    for s in range(init, n, step):
        p = clone(pipe).fit(X.iloc[:s], y[:s])
        pred[s:s + step] = p.predict(X.iloc[s:s + step])
    return pred


def _scores(y, pred):
    m = ~np.isnan(pred)
    if m.sum() < 3:
        return {"r2": None, "mae": None, "n": int(m.sum())}
    return {"r2": float(r2_score(y[m], pred[m])), "mae": float(mean_absolute_error(y[m], pred[m])),
            "n": int(m.sum())}


def _add_poa(df, lat, tilt):
    out = df.copy()
    out["poa"] = [dataset.poa_daily(h, d, lat, tilt) for h, d in zip(out["shortwave_radiation_sum"], out["date"])]
    out["poa2"] = out["poa"] ** 2
    return out


def fit_tilt(df, lat, target="generation_kwh"):
    """Inclinación efectiva (°) que mejor explica la producción, por walk-forward (grueso + fino)."""
    y = df[target].astype(float).values

    def err(t):
        d = _add_poa(df, lat, t)
        return _scores(y, _walk_forward(_linear(0.1), d[["poa", "poa2"]].astype(float), y))["mae"] or 1e9
    coarse = min(range(0, 61, 5), key=err)
    return float(min(range(max(0, coarse - 4), min(60, coarse + 4) + 1), key=err))


def train(df, features, target="generation_kwh", lat=None):
    data = df.dropna(subset=[target]).sort_values("date").reset_index(drop=True)
    # 1) fuera la fase de puesta en marcha (sin vertido: producción recortada, no depende del sol)
    cutoff = dataset.commissioning_cutoff(data)
    n_excl = 0
    if cutoff:
        n_excl = int((data["date"] < cutoff).sum())
        data = data[data["date"] >= cutoff].reset_index(drop=True)
    n = len(data)
    if n < 8:
        raise ValueError(f"Muy pocos días con datos ({n}). Necesitas al menos ~8 (y mejor varias semanas).")
    y = data[target].astype(float).values

    # 2) variable física: radiación en el plano inclinado, con la inclinación ajustada a los datos
    tilt = None
    features = [f for f in features if f not in ("poa", "poa2")]
    if lat is not None and "shortwave_radiation_sum" in data.columns and n >= 30:
        tilt = fit_tilt(data, lat, target)
        data = _add_poa(data, lat, tilt)
        features = features + ["poa", "poa2"]

    # 3) validación honesta de cada candidato
    weeks = ((pd.to_datetime(data["date"]) - pd.to_datetime(data["date"].min())).dt.days // 7).values
    k = min(5, max(2, len(set(weeks))))
    results = {}
    for name, (pipe, feats) in _candidates(n, features).items():
        X = data[feats].astype(float)
        wf = _walk_forward(pipe, X, y)
        blk = cross_val_predict(clone(pipe), X, y, cv=GroupKFold(n_splits=k), groups=weeks)
        results[name] = {"pipe": pipe, "feats": feats, "wf": wf, "blk": blk,
                         "s_wf": _scores(y, wf), "s_blk": _scores(y, blk)}

    def key(nm):       # se elige por error en walk-forward (lo que importa al predecir mañana)
        s = results[nm]["s_wf"]
        return s["mae"] if s["mae"] is not None else results[nm]["s_blk"]["mae"]
    best = min(results, key=key)
    rb = results[best]
    best_pipe = clone(rb["pipe"]).fit(data[rb["feats"]].astype(float), y)

    dates = list(data["date"].astype(str).values)
    backtest = [{"date": d, "actual": round(float(a), 2), "pred": round(float(max(0.0, p)), 2),
                 "wf": (None if np.isnan(w) else round(float(max(0.0, w)), 2))}
                for d, a, p, w in zip(dates, y, rb["blk"], rb["wf"])]

    # banda de incertidumbre: cuantiles del error real (walk-forward si hay, si no bloques)
    res = y - (rb["wf"] if rb["s_wf"]["n"] >= 20 else rb["blk"])
    res = res[~np.isnan(res)]
    resid_q = [float(np.quantile(res, 0.1)), float(np.quantile(res, 0.9))] if len(res) else [0.0, 0.0]

    importance = _importance(best_pipe, rb["feats"])
    if not importance:   # modelos sin importancia nativa (p. ej. HistGB) -> por permutación
        try:
            from sklearn.inspection import permutation_importance
            pi = permutation_importance(best_pipe, data[rb["feats"]].astype(float), y, n_repeats=8, random_state=0)
            vals = np.clip(pi.importances_mean, 0, None)
            s = vals.sum() or 1.0
            importance = {f: float(v / s) for f, v in sorted(zip(rb["feats"], vals), key=lambda t: -t[1])}
        except Exception:
            pass

    def pub(s):
        return {"r2": s["r2"], "mae": s["mae"], "n": s["n"]}
    metrics = {}
    for name, r in results.items():
        main = r["s_wf"] if r["s_wf"]["mae"] is not None else r["s_blk"]
        metrics[name] = {"r2": main["r2"], "mae": main["mae"],          # titular = walk-forward
                         "walk_forward": pub(r["s_wf"]), "bloques": pub(r["s_blk"])}

    return {
        "model": best_pipe,
        "model_name": best,
        "features": rb["feats"],
        "target": target,
        "n_samples": n,
        "n_excluded": n_excl,
        "cutoff": cutoff,
        "lat": lat,
        "tilt": tilt,
        "validation": "walk-forward",
        "metrics": metrics,
        "importance": importance,
        "y_mean": float(np.mean(y)),
        "backtest": backtest,
        "resid_q": resid_q,
    }


def _importance(pipe, features):
    try:
        step = pipe.named_steps["m"]
        if hasattr(step, "coef_"):
            vals = np.abs(step.coef_)
        elif hasattr(step, "feature_importances_"):
            vals = step.feature_importances_
        else:
            return {}
        s = vals.sum() or 1.0
        return {f: float(v / s) for f, v in sorted(zip(features, vals), key=lambda t: -t[1])}
    except Exception:
        return {}


def predict(bundle, feature_rows):
    """feature_rows: DataFrame o lista de dicts con el clima (+ 'date'). Las variables físicas
    (POA) se calculan aquí con la latitud e inclinación guardadas en el bundle."""
    if isinstance(feature_rows, list):
        feature_rows = pd.DataFrame(feature_rows)
    if bundle.get("tilt") is not None and "poa" in bundle["features"] and "poa" not in feature_rows.columns:
        feature_rows = _add_poa(feature_rows, bundle["lat"], bundle["tilt"])
    X = feature_rows[bundle["features"]].astype(float)
    out = np.asarray(bundle["model"].predict(X), dtype=float)
    # sin radiación no hay predicción que valga: NaN (no una mediana inventada por el imputador)
    if "shortwave_radiation_sum" in feature_rows.columns:
        out = np.where(pd.to_numeric(feature_rows["shortwave_radiation_sum"], errors="coerce").isna().values, np.nan, out)
    return out


def band(bundle, value):
    """(bajo, alto) al 80 % alrededor de una predicción, según el error real medido."""
    q = bundle.get("resid_q") or [0.0, 0.0]
    return max(0.0, value + q[0]), max(0.0, value + q[1])


def save(bundle, path):
    joblib.dump(bundle, path)


def load(path):
    return joblib.load(path)
