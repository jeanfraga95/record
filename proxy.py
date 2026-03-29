#!/usr/bin/env python3
"""
RecordPlus HLS Proxy  v2
========================
Links fixos para VLC:
  http://<IP>:8888/channel/sp
  http://<IP>:8888/channel/rio
  http://<IP>:8888/channel/minas

Diagnóstico:
  http://<IP>:8888/debug
"""

import re
import sys
import time
import threading
import logging
import traceback
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, Response, request as req, abort

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
EMAIL    = "jean.fraga20@gmail.com"
PASSWORD = "qwerty123"

PROFILE_ID   = "8a7ea0f8-c8c1-4a29-b424-14bbf7ee9275"
PROFILE_NAME = "jean"

CHANNELS = {
    "sp":    {"name": "Record SP",    "event_id": "180", "group_id": "7"},
    "rio":   {"name": "Record Rio",   "event_id": "182", "group_id": "7"},
    "minas": {"name": "Record Minas", "event_id": "186", "group_id": "7"},
}

PORT             = 8888
REFRESH_INTERVAL = 1800
BASE_URL         = "https://www.recordplus.com"
UA               = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("rp")
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)
try:
    _fh = logging.FileHandler("/var/log/recordplus-proxy.log")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except Exception:
    pass

debug_log: list = []

def _dbg(msg: str, level: str = "info") -> None:
    getattr(log, level)(msg)
    sys.stdout.flush()
    debug_log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(debug_log) > 300:
        debug_log.pop(0)

# ──────────────────────────────────────────────────────────────────────────────
# ESTADO
# ──────────────────────────────────────────────────────────────────────────────
streams: dict = {}
lock = threading.Lock()

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def _extract_m3u8(text: str):
    """Procura qualquer URL m3u8 dentro de uma string HTML/JSON."""
    patterns = [
        r'https://[^\s"\'<>\\]+master\.m3u8[^\s"\'<>\\]*',
        r'https://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(0).replace("\\/", "/")
    return None


def _make_akamai_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v)
    s.headers.update({
        "User-Agent": UA,
        "Referer":    BASE_URL + "/",
        "Origin":     BASE_URL,
    })
    return s


# ──────────────────────────────────────────────────────────────────────────────
# ABORDAGEM 1: requests puro
# ──────────────────────────────────────────────────────────────────────────────
def _fetch_via_requests():
    _dbg("[requests] Iniciando…")
    results: dict = {}

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

    # 1. GET login page → CSRF token
    _dbg("[requests] GET login page…")
    r = s.get(f"{BASE_URL}/account/login", timeout=20)
    r.raise_for_status()

    csrf = re.search(
        r'__RequestVerificationToken[^>]+value="([^"]+)"', r.text
    ) or re.search(
        r'name="__RequestVerificationToken"\s+[^>]*value="([^"]+)"', r.text
    )
    csrf_token = csrf.group(1) if csrf else ""
    _dbg(f"[requests] CSRF: {'encontrado' if csrf_token else 'NÃO encontrado'}")

    # 2. POST login
    _dbg("[requests] POST login…")
    r = s.post(
        f"{BASE_URL}/account/login",
        data={
            "__RequestVerificationToken": csrf_token,
            "UserName": EMAIL,
            "Password": PASSWORD,
        },
        timeout=20,
        allow_redirects=True,
    )
    _dbg(f"[requests] Após login → {r.url}  status={r.status_code}")

    if "escolhaseuperfil" not in r.url.lower() and "home" not in r.url.lower():
        raise Exception(f"Login falhou. URL final: {r.url}")

    # 3. Seleção de perfil
    if "escolhaseuperfil" in r.url.lower():
        _dbg("[requests] Selecionando perfil…")
        for ep in [
            f"{BASE_URL}/account/SelectProfile",
            f"{BASE_URL}/Account/SelectProfile",
            f"{BASE_URL}/account/escolhaseuperfil",
        ]:
            try:
                rp = s.post(
                    ep,
                    data={"profileId": PROFILE_ID, "ProfileId": PROFILE_ID},
                    timeout=15,
                    allow_redirects=True,
                )
                _dbg(f"[requests]   {ep} → {rp.status_code} {rp.url}")
                if rp.status_code < 400:
                    break
            except Exception as e:
                _dbg(f"[requests]   {ep} erro: {e}")

        s.get(f"{BASE_URL}/home", timeout=15, allow_redirects=True)

    # 4. Captura stream de cada canal
    for ch, info in CHANNELS.items():
        ch_url = f"{BASE_URL}/Live/LiveEvent/{info['event_id']}?groupId={info['group_id']}"
        _dbg(f"[requests] [{ch.upper()}] GET {ch_url}")
        try:
            r = s.get(ch_url, timeout=20)
            _dbg(f"[requests] [{ch.upper()}] {r.status_code} / {len(r.text)} chars")
            m3u8 = _extract_m3u8(r.text)
            if m3u8:
                _dbg(f"[requests] [{ch.upper()}] ✓ {m3u8[:80]}…")
                results[ch] = {
                    "master_url": m3u8,
                    "session":    _make_akamai_session(dict(s.cookies)),
                }
            else:
                _dbg(f"[requests] [{ch.upper()}] m3u8 não encontrado no HTML")
                # Salva HTML para debug
                debug_log.append(f"--- HTML snippet [{ch}] ---")
                debug_log.append(r.text[:500])
        except Exception as e:
            _dbg(f"[requests] [{ch.upper()}] ERRO: {e}")

    return results


# ──────────────────────────────────────────────────────────────────────────────
# ABORDAGEM 2: Playwright headless
# ──────────────────────────────────────────────────────────────────────────────
def _fetch_via_playwright():
    _dbg("[playwright] Importando…")
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa

    results: dict = {}

    with sync_playwright() as pw:
        _dbg("[playwright] Lançando Chromium…")
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
        )
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
        page = ctx.new_page()

        # Login
        _dbg("[playwright] GET login…")
        try:
            page.goto(f"{BASE_URL}/account/login",
                      wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            pass

        page.wait_for_timeout(2000)
        page.fill("#UserName", EMAIL)
        page.fill("#Password", PASSWORD)
        page.click("button[type=submit]")

        try:
            page.wait_for_url("**/escolhaseuperfil**", timeout=15000)
            _dbg(f"[playwright] Redirecionado: {page.url}")
        except PWTimeout:
            _dbg(f"[playwright] URL após submit: {page.url}")

        # Perfil
        if "escolhaseuperfil" in page.url.lower():
            _dbg("[playwright] Clicando no perfil…")
            try:
                page.click(f"img.profile-img[onclick*='{PROFILE_ID}']", timeout=5000)
            except Exception:
                try:
                    page.click("img.profile-img", timeout=5000)
                except Exception as e:
                    _dbg(f"[playwright] aviso clique perfil: {e}")
            try:
                page.wait_for_url("**/home**", timeout=15000)
            except PWTimeout:
                page.wait_for_timeout(3000)
            _dbg(f"[playwright] URL após perfil: {page.url}")

        # Captura por canal
        for ch, info in CHANNELS.items():
            ch_url = (f"{BASE_URL}/Live/LiveEvent/{info['event_id']}"
                      f"?groupId={info['group_id']}")
            captured: dict = {}

            def _on_req(r, _c=captured, _ch=ch):
                if "master.m3u8" in r.url and not _c.get("master_url"):
                    _c["master_url"] = r.url
                    _dbg(f"[playwright] [{_ch.upper()}] ✓ capturado: {r.url[:70]}")

            page.on("request", _on_req)
            _dbg(f"[playwright] [{ch.upper()}] Navegando…")
            try:
                page.goto(ch_url, wait_until="networkidle", timeout=25000)
            except PWTimeout:
                page.wait_for_timeout(3000)

            for _ in range(24):
                if "master_url" in captured:
                    break
                page.wait_for_timeout(500)

            page.remove_listener("request", _on_req)

            if "master_url" not in captured:
                _dbg(f"[playwright] [{ch.upper()}] network não capturou, tentando HTML…")
                m3u8 = _extract_m3u8(page.content())
                if m3u8:
                    captured["master_url"] = m3u8
                    _dbg(f"[playwright] [{ch.upper()}] ✓ extraído do HTML")
                else:
                    _dbg(f"[playwright] [{ch.upper()}] ✗ não encontrado")

            if "master_url" in captured:
                ak_s = _make_akamai_session(
                    {c["name"]: c["value"] for c in ctx.cookies()}
                )
                results[ch] = {"master_url": captured["master_url"], "session": ak_s}

        browser.close()

    return results


# ──────────────────────────────────────────────────────────────────────────────
# LOOP DE RENOVAÇÃO
# ──────────────────────────────────────────────────────────────────────────────
def fetch_streams() -> None:
    _dbg("=" * 50)
    _dbg("CAPTURA DE STREAMS INICIADA")
    _dbg("=" * 50)

    new: dict = {}

    # Tenta requests primeiro
    try:
        new = _fetch_via_requests()
        _dbg(f"[requests] capturados: {list(new.keys())}")
    except Exception:
        _dbg("[requests] FALHA:\n" + traceback.format_exc())

    # Se faltou algum, tenta Playwright
    missing = [ch for ch in CHANNELS if ch not in new]
    if missing:
        _dbg(f"Faltam {missing} — tentando Playwright…")
        try:
            pw = _fetch_via_playwright()
            new.update(pw)
            _dbg(f"[playwright] capturados: {list(pw.keys())}")
        except Exception:
            _dbg("[playwright] FALHA:\n" + traceback.format_exc())

    if new:
        with lock:
            streams.clear()
            streams.update(new)
        _dbg(f"✅ Streams ativos: {list(new.keys())}")
    else:
        _dbg("❌ Nenhum stream — acesse /debug para ver o log completo")


def _refresh_loop() -> None:
    while True:
        try:
            fetch_streams()
        except Exception:
            _dbg("ERRO CRÍTICO:\n" + traceback.format_exc())
        _dbg(f"Próxima renovação em {REFRESH_INTERVAL // 60} min")
        time.sleep(REFRESH_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# PROXY HLS
# ──────────────────────────────────────────────────────────────────────────────
def _proxy_url(url: str) -> str:
    return f"/proxy?u={quote(url, safe='')}"


def _rewrite_m3u8(content: str, base_url: str) -> str:
    parsed    = urlparse(base_url)
    base_root = f"{parsed.scheme}://{parsed.netloc}"
    out = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            out.append(line)
        elif s.startswith("http"):
            out.append(_proxy_url(s))
        else:
            out.append(_proxy_url(base_root + "/" + s.lstrip("/")))
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────────
# FLASK
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    host = req.host.split(":")[0]
    rows = "".join(
        f"<tr><td><b>{ch.upper()}</b></td>"
        f"<td>{CHANNELS[ch]['name']}</td>"
        f"<td><code>http://{host}:{PORT}/channel/{ch}</code></td>"
        f"<td>{'✅ Pronto' if ch in streams else '⏳ Aguardando'}</td></tr>"
        for ch in CHANNELS
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>RecordPlus Proxy</title></head>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>📺 RecordPlus HLS Proxy v2</h2>"
        "<p>Use os links abaixo no VLC via <b>Mídia → Abrir fluxo de rede</b></p>"
        "<table border='1' cellpadding='8'>"
        "<tr><th>ID</th><th>Canal</th><th>URL p/ VLC</th><th>Status</th></tr>"
        f"{rows}</table>"
        "<p><a href='/debug'>🔍 Ver log de diagnóstico</a></p>"
        "</body></html>"
    )


@app.route("/debug")
def debug():
    with lock:
        active = {ch: streams[ch]["master_url"][:90] + "…" for ch in streams}
    lines = "\n".join(debug_log[-150:])
    return Response(
        f"STREAMS ATIVOS:\n{active}\n\n{'='*50}\nLOG:\n{lines}",
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/channel/<ch>")
def channel(ch: str):
    if ch not in CHANNELS:
        abort(404)
    with lock:
        info = streams.get(ch)
    if not info:
        return Response(
            "Stream ainda não disponível. Aguarde ~60 s e tente novamente.\n"
            f"Diagnóstico: http://{req.host}/debug",
            status=503,
            mimetype="text/plain",
        )
    try:
        r = info["session"].get(info["master_url"], timeout=12)
        r.raise_for_status()
    except Exception as e:
        _dbg(f"[{ch}] Erro upstream: {e}")
        abort(502)

    return Response(
        _rewrite_m3u8(r.text, info["master_url"]),
        mimetype="application/x-mpegURL",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@app.route("/proxy")
def proxy():
    url = unquote(req.args.get("u", ""))
    if not url:
        abort(400)
    with lock:
        sessions = [v["session"] for v in streams.values()]
    if not sessions:
        abort(503)
    try:
        up = sessions[0].get(url, stream=True, timeout=15)
        up.raise_for_status()
    except Exception as e:
        _dbg(f"proxy erro: {e}", "warning")
        abort(502)

    ct = up.headers.get("Content-Type", "application/octet-stream")
    if "mpegURL" in ct or url.split("?")[0].endswith(".m3u8"):
        return Response(
            _rewrite_m3u8(up.text, url),
            mimetype="application/x-mpegURL",
            headers={"Cache-Control": "no-cache"},
        )

    def _stream():
        for chunk in up.iter_content(65536):
            if chunk:
                yield chunk

    return Response(_stream(), mimetype=ct,
                    headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"})


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50, flush=True)
    print(f"  RecordPlus Proxy v2  |  porta {PORT}", flush=True)
    print("=" * 50, flush=True)

    t = threading.Thread(target=_refresh_loop, daemon=True, name="refresh")
    t.start()

    print("Aguardando captura inicial (máx 90 s)…", flush=True)
    for _ in range(180):
        time.sleep(0.5)
        with lock:
            if streams:
                break

    print(f"Streams: {list(streams.keys()) or 'nenhum — acesse /debug'}", flush=True)
    print(f"Iniciando Flask em 0.0.0.0:{PORT}", flush=True)
    sys.stdout.flush()

    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
