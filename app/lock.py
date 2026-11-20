"""
Candado para la web publicada: cifra el HTML completo con usuario + contraseña.

GitHub Pages solo sirve archivos estáticos (no hay servidor que pida login), así que la
protección real es criptográfica: el archivo publicado solo contiene datos cifrados
(AES-256-GCM, clave PBKDF2-SHA256 de 600 000 iteraciones). Sin la contraseña no se ve nada,
ni siquiera mirando el código fuente. El navegador descifra con WebCrypto.

Credenciales en clave-web.json (fuera de web/, no se publica):
    {"usuario": "...", "contrasena": "..."}
o, para reutilizar las de Deye de config.json (desaconsejado):
    {"usar_deye": true}
"""
import base64
import gzip
import json
import os

ITER = 600_000
HERE = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = os.path.join(HERE, "clave-web.json")


def load_credentials(cfg):
    """(usuario, contraseña) o None si no hay candado configurado."""
    if not os.path.exists(CRED_FILE):
        return None
    with open(CRED_FILE, encoding="utf-8") as fh:
        c = json.load(fh)
    if c.get("usar_deye"):
        u, p = cfg.get("email"), cfg.get("password")
    else:
        u, p = c.get("usuario"), c.get("contrasena")
    if not u or not p:
        raise RuntimeError("clave-web.json incompleto (faltan usuario o contraseña)")
    return str(u), str(p)


def _material(usuario, contrasena):
    # el usuario forma parte de la clave: hay que acertar los dos
    return (usuario.strip().lower() + "\n" + contrasena).encode("utf-8")


def _salt(usuario, contrasena):
    """Sal fija mientras no cambien las credenciales: así «Recordar en este dispositivo»
    sobrevive a las actualizaciones diarias. Si cambian, sal nueva (y fuera recordatorios)."""
    import hashlib
    path = os.path.join(HERE, "clave-web.salt")
    tag = hashlib.sha256(b"tag" + _material(usuario, contrasena)).hexdigest()[:16]
    try:
        with open(path, encoding="ascii") as fh:
            t, s = fh.read().split()
        if t == tag:
            return bytes.fromhex(s)
    except (OSError, ValueError):
        pass
    salt = os.urandom(16)
    with open(path, "w", encoding="ascii") as fh:
        fh.write(tag + " " + salt.hex())
    return salt


def encrypt_page(html, usuario, contrasena):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv = _salt(usuario, contrasena), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(
        _material(usuario, contrasena))
    ct = AESGCM(key).encrypt(iv, gzip.compress(html.encode("utf-8"), 9), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    payload = json.dumps({"s": b64(salt), "i": b64(iv), "n": ITER, "c": b64(ct)})
    return LOCK_PAGE.replace("__PAYLOAD__", payload)


LOCK_PAGE = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Panel solar</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2433;--muted:#667085;--line:#d9dde5;--acc:#e8a317;--bad:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#11151c;--card:#1a2029;--fg:#e8ebf0;--muted:#98a2b3;--line:#2d3542;--bad:#ef6b5b}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
  font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}
form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px 24px}
h1{margin:0 0 4px;font-size:1.375rem;letter-spacing:-.02em}
p{margin:0 0 20px;color:var(--muted);font-size:.875rem}
label{display:block;font-size:.75rem;color:var(--muted);margin:12px 0 4px}
input[type=text],input[type=password]{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:8px;
  background:transparent;color:var(--fg);font-size:1rem}
input:focus{outline:2px solid var(--acc);outline-offset:1px}
.rem{display:flex;gap:8px;align-items:center;margin:14px 0 0;font-size:.875rem;color:var(--muted)}
button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:8px;background:var(--acc);color:#1d1d1d;
  font-weight:600;font-size:1rem;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
.err{color:var(--bad);font-size:.875rem;min-height:1.3em;margin-top:10px}
</style></head><body>
<form id="f" autocomplete="on">
  <h1>☀ Panel solar</h1>
  <p>Acceso privado</p>
  <label for="u">Usuario</label><input id="u" type="text" autocomplete="username" autocapitalize="none" required>
  <label for="p">Contraseña</label><input id="p" type="password" autocomplete="current-password" required>
  <label class="rem"><input id="r" type="checkbox"> Recordar en este dispositivo</label>
  <button id="b">Entrar</button>
  <div class="err" id="e" role="alert"></div>
</form>
<script>
const P=__PAYLOAD__, KEY="panelsolar.k";
const d64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function unlock(raw){
  const key=await crypto.subtle.importKey("raw",raw,"AES-GCM",false,["decrypt"]);
  const gz=await crypto.subtle.decrypt({name:"AES-GCM",iv:d64(P.i)},key,d64(P.c));
  return await new Response(new Blob([gz]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
}
function show(html){document.open();document.write(html);document.close();}
async function derive(u,p){
  const m=new TextEncoder().encode(u.trim().toLowerCase()+"\n"+p);
  const base=await crypto.subtle.importKey("raw",m,"PBKDF2",false,["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({name:"PBKDF2",hash:"SHA-256",salt:d64(P.s),iterations:P.n},base,256));
}
// clave en sessionStorage siempre (el botón «Recargar» no vuelve a pedirla en esta pestaña);
// en localStorage solo con «Recordar en este dispositivo»
const st=w=>({get(){try{return window[w].getItem(KEY)}catch(e){return null}},
  set(v){try{window[w].setItem(KEY,v)}catch(e){}},del(){try{window[w].removeItem(KEY)}catch(e){}}});
const ss=st("sessionStorage"),lsL=st("localStorage");
const ls={get(){return ss.get()||lsL.get()},set(v,rem){ss.set(v);if(rem)lsL.set(v)},del(){ss.del();lsL.del()}};
if(!(window.crypto&&crypto.subtle&&window.DecompressionStream)){
  document.getElementById("e").textContent="Este navegador no permite descifrar el panel (ábrelo por https y en un navegador actualizado).";
  document.getElementById("b").disabled=true;
}
// clave recordada (no la contraseña): vale mientras no se cambie la contraseña (la sal es fija)
(async()=>{const s=ls.get();if(!s)return;
  try{const o=JSON.parse(s);if(o.s!==P.s){ls.del();return;} show(await unlock(d64(o.k)));}catch(e){ls.del();}})();
document.getElementById("f").addEventListener("submit",async ev=>{
  ev.preventDefault();const b=document.getElementById("b"),e=document.getElementById("e");
  b.disabled=true;b.textContent="Comprobando…";e.textContent="";
  let html=null,k=null;
  try{ k=await derive(document.getElementById("u").value,document.getElementById("p").value); html=await unlock(k); }catch(x){}
  if(html===null){e.textContent="Usuario o contraseña incorrectos.";b.disabled=false;b.textContent="Entrar";return;}
  ls.set(JSON.stringify({s:P.s,k:btoa(String.fromCharCode(...k))}),document.getElementById("r").checked);
  show(html);
});
</script></body></html>"""
