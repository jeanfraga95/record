#!/usr/bin/env python3
"""
RecordPlus HLS Proxy
====================
Faz login automático, captura os tokens Akamai e expõe URLs fixas para VLC.

Links fixos gerados:
  http://<IP_VPS>:8888/channel/sp
  http://<IP_VPS>:8888/channel/rio
  http://<IP_VPS>:8888/channel/minas

Os tokens são renovados automaticamente a cada 30 minutos.
"""

import threading
import time
import logging
import sys
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, Response, request as req, abort

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURAÇÃO
# ──────────────────────────────────────────────────────────────────────────────
EMAIL    = "jean.fraga20@gmail.com"
PASSWORD = "qwerty123"

CHANNELS = {
    "sp":    "https://www.recordplus.com/Live/LiveEvent/180?groupId=7",
    "rio":   "https://www.recordplus.com/live/liveEvent/182?groupId=7",
    "minas": "https://www.recordplus.com/live/liveEvent/186?groupId=7",
}

PORT             = 8888
REFRESH_INTERVAL = 1800   # renova tokens a cada 30 min
CAPTURE_TIMEOUT  = 12000  # ms que o Playwright aguarda o player iniciar

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/recordplus-proxy.log"),
    ],
)
log = logging.getLogger("recordplus")

# ──────────────────────────────────────────────────────────────────────────────
# ESTADO GLOBAL
# ──────────────────────────────────────────────────────────────────────────────
streams: dict = {}   # ch → {"master_url": str, "session": requests.Session}
lock = threading.Lock()

# ──────────────────────────────────────────────────────────────────────────────
# CAPTURA DE STREAMS (Playwright headless)
# ──────────────────────────────────────────────────────────────────────────────

def _make_session(ctx) -> requests.Session:
    """Cria uma requests.Session com os cookies Akamai do contexto do browser."""
    s = requests.Session()
    for c in ctx.cookies():
        if "akamai" in c.get("domain", "") or c["name"] in ("hdntl", "hdnts"):
            s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.recordplus.com/",
        "Origin":  "https://www.recordplus.com",
    })
    return s


def fetch_streams() -> None:
    """Abre o browser, faz login e captura a URL master.m3u8 de cada canal."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    new_streams: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = ctx.new_page()

        # ── 1. Login ──────────────────────────────────────────────────────
        log.info("Acessando página de login…")
        try:
            page.goto(
                "https://www.recordplus.com/account/login",
                wait_until="networkidle",
                timeout=30000,
            )
        except PWTimeout:
            page.goto("https://www.recordplus.com/account/login")
            page.wait_for_timeout(3000)

        page.fill("#UserName", EMAIL)
        page.fill("#Password", PASSWORD)
        page.click("button[type=submit]")

        try:
            page.wait_for_url("**/escolhaseuperfil**", timeout=15000)
        except PWTimeout:
            log.error("Não redirecionou para seleção de perfil — verifique credenciais.")
            browser.close()
            return

        # ── 2. Seleciona perfil ──────────────────────────────────────────
        log.info("Selecionando perfil…")
        try:
            # Tenta clicar no perfil 'jean' especificamente
            page.click("img.profile-img[onclick*='jean']", timeout=5000)
        except Exception:
            # Fallback: clica no primeiro perfil disponível
            page.click("img.profile-img", timeout=5000)

        try:
            page.wait_for_url("**/home**", timeout=15000)
        except PWTimeout:
            # Às vezes redireciona direto para outra URL
            page.wait_for_timeout(4000)

        log.info("Login OK. Capturando streams…")

        # ── 3. Captura por canal ──────────────────────────────────────────
        for ch, ch_url in CHANNELS.items():
            log.info(f"  [{ch.upper()}] Navegando para {ch_url}")
            captured: dict = {}

            def _on_req(r, _cap=captured, _ch=ch):
                if "master.m3u8" in r.url and "akamai" in r.url:
                    if "master_url" not in _cap:
                        _cap["master_url"] = r.url
                        log.info(f"  [{_ch.upper()}] ✓ master.m3u8 capturado")

            page.on("request", _on_req)

            try:
                page.goto(ch_url, wait_until="networkidle", timeout=25000)
            except PWTimeout:
                page.wait_for_timeout(3000)

            # Aguarda o player iniciar e emitir a requisição m3u8
            deadline = time.time() + CAPTURE_TIMEOUT / 1000
            while "master_url" not in captured and time.time() < deadline:
                page.wait_for_timeout(500)

            page.remove_listener("request", _on_req)

            if "master_url" not in captured:
                log.warning(f"  [{ch.upper()}] master.m3u8 NÃO capturado — pulando.")
                continue

            session = _make_session(ctx)
            new_streams[ch] = {
                "master_url": captured["master_url"],
                "session":    session,
            }

        browser.close()

    if new_streams:
        with lock:
            streams.clear()
            streams.update(new_streams)
        log.info(f"Streams prontos: {list(new_streams.keys())}")
    else:
        log.error("Nenhum stream capturado nesta rodada.")


def _refresh_loop() -> None:
    """Thread em background que renova os tokens periodicamente."""
    while True:
        try:
            fetch_streams()
        except Exception as exc:
            log.exception(f"Erro ao renovar streams: {exc}")
        log.info(f"Próxima renovação em {REFRESH_INTERVAL // 60} minutos.")
        time.sleep(REFRESH_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# PROXY HLS
# ──────────────────────────────────────────────────────────────────────────────

def _proxy_url(url: str) -> str:
    """Retorna a URL local do proxy para uma URL upstream."""
    return f"/proxy?u={quote(url, safe='')}"


def _rewrite_m3u8(content: str, base_url: str) -> str:
    """Reescreve URLs dentro de um arquivo m3u8 para passar pelo proxy local."""
    parsed   = urlparse(base_url)
    base_root = f"{parsed.scheme}://{parsed.netloc}"
    lines = content.splitlines()
    out   = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue

        if stripped.startswith("http://") or stripped.startswith("https://"):
            out.append(_proxy_url(stripped))
        else:
            # caminho relativo → absoluto
            abs_url = base_root + "/" + stripped.lstrip("/")
            out.append(_proxy_url(abs_url))

    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    host = req.host.split(":")[0]
    rows = "\n".join(
        f"<tr><td><b>{ch.upper()}</b></td>"
        f"<td><a href='http://{host}:{PORT}/channel/{ch}'>"
        f"http://{host}:{PORT}/channel/{ch}</a></td>"
        f"<td>{'✅ Pronto' if ch in streams else '⏳ Aguardando'}</td></tr>"
        for ch in CHANNELS
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>RecordPlus Proxy</title></head>
<body style='font-family:sans-serif;padding:2em'>
<h2>📺 RecordPlus HLS Proxy</h2>
<p>Abra os links abaixo no VLC: <b>Mídia → Abrir fluxo de rede</b></p>
<table border='1' cellpadding='8' cellspacing='0'>
<tr><th>Canal</th><th>URL para VLC</th><th>Status</th></tr>
{rows}
</table>
<p><small>Tokens renovam automaticamente a cada {REFRESH_INTERVAL//60} minutos.</small></p>
</body></html>"""


@app.route("/channel/<ch>")
def channel(ch: str):
    if ch not in CHANNELS:
        abort(404, f"Canal '{ch}' não existe. Disponíveis: {list(CHANNELS)}")

    with lock:
        info = streams.get(ch)

    if not info:
        abort(503, "Stream ainda não disponível. Aguarde o proxy inicializar (~30 s).")

    master_url = info["master_url"]
    session    = info["session"]

    try:
        r = session.get(master_url, timeout=10)
        r.raise_for_status()
    except Exception as exc:
        log.error(f"[{ch}] Erro ao buscar master.m3u8: {exc}")
        abort(502, "Erro ao buscar stream upstream.")

    content = _rewrite_m3u8(r.text, master_url)
    return Response(
        content,
        mimetype="application/x-mpegURL",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@app.route("/proxy")
def proxy():
    raw = req.args.get("u", "")
    if not raw:
        abort(400, "Parâmetro 'u' obrigatório.")
    url = unquote(raw)

    # Pega a primeira session disponível (todas têm os mesmos cookies Akamai)
    with lock:
        sessions = [v["session"] for v in streams.values()]

    if not sessions:
        abort(503, "Proxy ainda inicializando.")

    session = sessions[0]

    try:
        upstream = session.get(url, stream=True, timeout=15)
        upstream.raise_for_status()
    except Exception as exc:
        log.warning(f"Proxy erro para {url[:80]}: {exc}")
        abort(502, str(exc))

    ct = upstream.headers.get("Content-Type", "application/octet-stream")

    if "mpegURL" in ct or url.endswith(".m3u8"):
        # Playlist aninhada → reescreve também
        content = _rewrite_m3u8(upstream.text, url)
        return Response(
            content,
            mimetype="application/x-mpegURL",
            headers={"Cache-Control": "no-cache"},
        )
    else:
        # Segmento TS / binário → stream direto
        def _stream():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(
            _stream(),
            mimetype=ct,
            headers={
                "Cache-Control": "no-cache",
                "Accept-Ranges":  "bytes",
            },
        )


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  RecordPlus HLS Proxy  |  porta %d", PORT)
    log.info("=" * 60)

    t = threading.Thread(target=_refresh_loop, daemon=True, name="token-refresh")
    t.start()

    # Aguarda a primeira captura antes de levantar o Flask
    log.info("Aguardando captura inicial de streams…")
    for _ in range(120):          # até 60 s
        time.sleep(0.5)
        with lock:
            if streams:
                break

    log.info("Servidor Flask iniciando em 0.0.0.0:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
