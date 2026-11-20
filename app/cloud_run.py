"""
Actualización en la nube (GitHub Actions): caja fuerte -> histórico Deye -> reentreno -> panel cifrado.

Los registros de GitHub Actions de un repositorio público los puede leer cualquiera, así que aquí
NO se imprime nada de los pasos internos (llevan id de planta y coordenadas): solo un resumen limpio.
Salida: <raíz del repo>/_site/index.html (lo publica el workflow en GitHub Pages).
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SITE = os.path.join(ROOT, "_site")


def main():
    email, pw = os.environ.get("DEYE_EMAIL", ""), os.environ.get("DEYE_PASSWORD", "")
    if not email or not pw:
        sys.exit("Faltan los secretos DEYE_EMAIL / DEYE_PASSWORD en Settings -> Secrets and variables -> Actions.")
    sys.path.insert(0, HERE)
    import vault
    with open(os.path.join(HERE, "vault.bin"), "rb") as fh:
        content = vault.unpack(fh.read(), email, pw)
    cfg = content["config"]
    secretos = [str(cfg.get(k) or "") for k in ("appId", "appSecret", "email", "password", "password_sha256",
                                                 "stationId", "deviceSn")]
    secretos.append(os.environ.get("PANEL_DISPATCH_TOKEN", ""))
    secretos = [s for s in secretos if len(s) >= 4]

    def w(name, data):
        with open(os.path.join(HERE, name), "w", encoding="utf-8") as fh:
            fh.write(data)
    w("config.json", json.dumps(cfg, ensure_ascii=False))
    w("clave-web.json", json.dumps({"usar_deye": True}))
    if content.get("salt"):
        w("clave-web.salt", content["salt"])

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("DEYE_EMAIL", None); env.pop("DEYE_PASSWORD", None)
    pasos = [("Histórico Deye + clima", ["cli.py", "fetch-history", "--days", "400"]),
             ("Reentreno del modelo", ["cli.py", "train"]),
             ("Panel cifrado", ["snapshot.py", "--web"])]
    for nombre, args in pasos:
        r = subprocess.run([sys.executable] + args, cwd=HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            ult = (r.stderr.strip() or r.stdout.strip() or "sin detalle").splitlines()[-1][:300]
            for s in secretos:
                ult = ult.replace(s, "***")
            ult = re.sub(r"https?://\S+", "<url>", ult)              # las URLs llevan coordenadas / ids
            ult = re.sub(r"-?\d{1,3}\.\d{3,}", "<num>", ult)         # y cualquier coordenada suelta
            sys.exit(f"ERROR en «{nombre}»: {ult}")
        print(f"OK  {nombre}")

    os.makedirs(SITE, exist_ok=True)
    with open(os.path.join(HERE, "snapshot-panel.html"), encoding="utf-8") as fh:
        html = fh.read()
    if "Acceso privado" not in html or "__SNAP__" in html:
        sys.exit("ERROR: el panel no salió cifrado; no se publica.")
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Listo: panel cifrado de {len(html) // 1024} KB preparado para publicar.")


if __name__ == "__main__":
    main()
