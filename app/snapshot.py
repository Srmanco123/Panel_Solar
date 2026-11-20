"""
Genera una INSTANTÁNEA autocontenida del panel (para abrir en el móvil / publicar en web sin servidor).
Reutiliza los endpoints reales vía test_client y hornea los datos dentro del HTML como
window.__SNAP__; la propia web detecta esa variable y lee de ahí en vez de llamar al servidor.

Lo que se publica NO lleva: credenciales, id de planta, nombre/dirección ni coordenadas exactas.
"""
import json
import os
import sys
import datetime as dt

import server

HERE = os.path.dirname(os.path.abspath(__file__))
c = server.app.test_client()


def jget(path, required=False):
    r = c.get(path)
    data = r.get_json(silent=True)
    if r.status_code != 200 or data is None:
        if required:
            raise RuntimeError(f"{path} -> {r.status_code}: {(data or {}).get('error', 'sin respuesta')}")
        return None
    return data


BTN = ('<button type="button" id="{id}" style="font:600 .8rem system-ui,sans-serif;padding:7px 14px;'
       'border-radius:8px;border:1px solid var(--line);background:var(--surface);color:var(--fg);cursor:pointer">'
       '{txt}</button>')

# Lanza el workflow en GitHub, sigue su progreso y recarga al terminar (~2 min).
DISPATCH_JS = r"""<script>(function(){
var C=__CFG__,b=document.getElementById("snap-now"),m=document.getElementById("snap-msg");
var H={"Authorization":"Bearer "+C.t,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"};
var API="https://api.github.com/repos/"+C.r+"/actions";
function say(t,bad){m.style.display="block";m.style.color=bad?"var(--bad,#c0392b)":"inherit";m.textContent=t;}
function reset(){b.disabled=false;b.textContent="⚡ Actualizar ahora";}
var sleep=function(ms){return new Promise(function(r){setTimeout(r,ms)})};
b.onclick=async function(){
  b.disabled=true;b.textContent="Pidiendo…";
  var t0=Date.now()-15000,run=null,start=Date.now();
  try{
    var r=await fetch(API+"/workflows/"+C.w+"/dispatches",{method:"POST",headers:H,body:JSON.stringify({ref:"main"})});
    if(r.status!==204) throw new Error(r.status===401||r.status===403?"el permiso (token) ha caducado o no es válido":"GitHub respondió "+r.status);
    while(Date.now()-start<8*60000){
      await sleep(8000);
      var j=await (await fetch(API+"/workflows/"+C.w+"/runs?event=workflow_dispatch&per_page=5",{headers:H,cache:"no-store"})).json();
      run=(j.workflow_runs||[]).filter(function(x){return Date.parse(x.created_at)>=t0;}).pop()||run;
      var s=Math.round((Date.now()-start)/1000);
      if(!run){say("Esperando a que GitHub arranque… ("+s+" s)");continue;}
      if(run.status!=="completed"){b.textContent="Actualizando…";say((run.status==="queued"?"En cola":"Descargando datos de Deye y regenerando el panel")+"… ("+s+" s, suele tardar 1–2 min)");continue;}
      if(run.conclusion!=="success") throw new Error("la actualización falló en GitHub ("+run.conclusion+")");
      say("¡Listo! Cargando los datos nuevos…");await sleep(4000);
      location.replace(location.pathname+"?t="+Date.now());return;
    }
    throw new Error("está tardando demasiado; prueba «Recargar» en unos minutos");
  }catch(e){say("No se pudo actualizar: "+e.message,true);reset();}
};})();</script>"""


def build():
    today = dt.date.today()
    year = today.year
    overview = jget("/api/overview", required=True)
    # anonimizar: sin nombre/dirección, sin id, coordenadas redondeadas (~10 km)
    st = overview.get("station") or {}
    st["name"] = "Mi instalación solar"
    st.pop("id", None)
    for k in ("lat", "lon"):
        if st.get(k) is not None:
            st[k] = round(float(st[k]), 1)
    yr = jget(f"/api/year?y={year}", required=True)
    months = {}
    for row in (yr.get("rows") or []):
        ym = f"{year}-{int(row['label']):02d}"
        m = jget(f"/api/month?ym={ym}")
        if m:
            months[ym] = m
    days = {}
    for i in range(0, 15):          # hoy (en curso) + últimos 14 días con curva intradía
        d = (today - dt.timedelta(days=i)).isoformat()
        dd = jget(f"/api/day?date={d}")
        if dd and dd.get("series"):
            days[d] = dd
    model = jget("/api/model")
    if model:
        model.pop("lat", None)
    data = {
        "overview": overview,
        "year": yr, "yearNum": year,
        "months": months,
        "days": days,
        "forecast": jget("/api/forecast?days=7"),
        "model": model,
        "prices": jget("/api/prices"),
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "dayList": sorted(days.keys()),
    }

    html = open(os.path.join(HERE, "webapp", "index.html"), encoding="utf-8").read()
    # 1) datos horneados, antes del script principal. "</" escapado para que ningún texto cierre el <script>.
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    marker = "<script>\nconst css="
    assert html.count(marker) == 1, "no encontré dónde inyectar los datos"
    html = html.replace(marker, "<script>window.__SNAP__=" + blob + ";</script>\n" + marker)
    # 2) aviso de instantánea antes de los KPIs
    # «Actualizar ahora»: solo en la web cifrada y si GitHub Actions nos pasa un token que únicamente puede
    # lanzar la actualización de este repo (va dentro del panel cifrado: solo lo tiene quien entra con la clave)
    tok, repo = os.environ.get("PANEL_DISPATCH_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    dispatch = {"t": tok, "r": repo, "w": "actualizar.yml"} if ("--web" in sys.argv and tok and repo) else None
    dias =", ".join(f"{d[8:10]}/{d[5:7]}" for d in data["dayList"]) or "—"
    gen_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    banner = ('<div style="background:var(--surface2);color:var(--fg);border:1px solid var(--line);border-radius:10px;'
              'padding:8px 12px;margin:8px 0;font:500 .78rem system-ui,sans-serif;display:flex;gap:10px;'
              'align-items:center;flex-wrap:wrap"><span style="flex:1 1 260px">Instantánea · datos del '
              + data["generated"] + ' <b id="snap-age" data-gen="' + gen_iso + '"></b> · se actualiza sola cada hora (06–23 h) · '
              'curva intradía: ' + dias + '. Los precios se cambian en la app del PC.</span>'
              + BTN.format(id="snap-reload", txt="↻ Recargar")
              + (BTN.format(id="snap-now", txt="⚡ Actualizar ahora") if dispatch else "")
              + '<span id="snap-msg" style="flex-basis:100%;display:none"></span></div>'
              '<script>(function(){var a=document.getElementById("snap-age"),g=new Date(a.dataset.gen);'
              'function age(){var m=Math.round((Date.now()-g)/60000);a.textContent=m<1?"(ahora)":m<60?"(hace "+m+" min)":'
              '"(hace "+Math.floor(m/60)+" h "+(m%60)+" min)";}age();setInterval(age,60000);'
              'document.getElementById("snap-reload").onclick=function(){this.disabled=true;this.textContent="Cargando…";'
              'location.replace(location.pathname+"?t="+Date.now());};})();</script>'
              + (DISPATCH_JS.replace("__CFG__", json.dumps(dispatch)) if dispatch else ""))
    assert '<div class="kpis" id="kpis">' in html
    html = html.replace('<div class="kpis" id="kpis">', banner + '<div class="kpis" id="kpis">', 1)

    # 3) cinturón de seguridad: nada sensible puede acabar en el archivo publicado
    cfg = server.S()["cfg"]
    secretos = [cfg.get(k) for k in ("appSecret", "password", "password_sha256", "email", "appId")]
    secretos += [str(cfg.get("stationId") or ""), str(server.S()["sid"]), str(cfg.get("deviceSn") or "")]
    for sec in secretos:
        if sec and len(str(sec)) >= 6 and str(sec) in html:
            raise RuntimeError("ABORTADO: la instantánea contendría un dato privado de config.json")

    # 4) candado (usuario + contraseña) para la web publicada
    if "--web" in sys.argv:
        import lock
        cred = lock.load_credentials(cfg)
        if not cred:
            raise RuntimeError("falta clave-web.json: la web no se publica sin usuario y contraseña")
        html = lock.encrypt_page(html, *cred)
        for sec in secretos + list(cred) + ([dispatch["t"]] if dispatch else []):
            if sec and len(str(sec)) >= 4 and str(sec) in html:
                raise RuntimeError("ABORTADO: el candado contendría un dato privado")
        print("Candado: cifrado con usuario y contraseña (AES-256-GCM, PBKDF2 600 000 it.)")

    out = os.path.join(HERE, "snapshot-panel.html")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, out)
    print("OK ->", out, f"({len(html) // 1024} KB)")
    print("meses:", list(months.keys()))
    print("días con intradía:", data["dayList"])


if __name__ == "__main__":
    try:
        build()
    except Exception as e:
        print("ERROR al generar la instantánea:", e)
        sys.exit(1)
