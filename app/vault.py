"""
Caja fuerte para la nube: config.json (+ sal del candado) cifrados con tu usuario y contraseña de Deye.

En GitHub solo se guarda vault.bin (ilegible). Al ejecutarse, GitHub Actions recibe el email y la
contraseña de Deye desde los «Secrets» del repositorio, abre la caja y reconstruye config.json en el
disco temporal de la máquina (que se borra al terminar).
Formato: b"DSV1" + sal(16) + iv(12) + AES-256-GCM(json).
"""
import json
import os

import lock

MAGIC = b"DSV1"


def _key(email, password, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=lock.ITER).derive(
        lock._material(email, password))


def pack(content: dict, email, password) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(email, password, salt)).encrypt(iv, json.dumps(content, ensure_ascii=False).encode("utf-8"), None)
    return MAGIC + salt + iv + ct


def unpack(blob: bytes, email, password) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:4] != MAGIC:
        raise ValueError("vault.bin no tiene el formato esperado")
    salt, iv, ct = blob[4:20], blob[20:32], blob[32:]
    try:
        return json.loads(AESGCM(_key(email, password, salt)).decrypt(iv, ct, None))
    except InvalidTag:
        raise ValueError("email o contraseña de Deye incorrectos (no abren la caja fuerte)") from None
