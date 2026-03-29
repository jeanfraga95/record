#!/usr/bin/env python3
"""
RecordPlus HLS Proxy  v3
========================
Links fixos para VLC:
  http://<IP>:8888/channel/sp
  http://<IP>:8888/channel/rio
  http://<IP>:8888/channel/minas

Diagnostico: http://<IP>:8888/debug
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

# ── CONFIG ────────────────────────────────────────────────────────────────────
EMAIL        = "jean.fraga20@gmail.com"
PASSWORD     = "qwerty123"
PROFILE_ID   = "8a7ea0f8-c8c1-4a29-b424-14bbf7ee9275"

CHANNELS = {
    "sp":    {"name": "Record SP",    "event_id": "180", "group_id": "7"},
    "rio":   {"name": "Record Rio",   "event_id": "182", "group_id": "7"},
    "minas": {"name": "Record Minas", "event_id": "186", "group_id": "7"},
}

PORT             = 8888
REFRESH_INTERVAL = 1500   # 25 min
BASE_URL         = "https://www.recordplus.com"
UA               = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36")

# ── LOGGING ───────────────────────────────────────────────────────────────────
log  = logging.getLogger("rp")
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

debug_log = []

def _dbg(msg, level="info"):
    getattr(log, level)(msg)
    sys.stdout.flush()
    debug_log.append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
    if len(debug_log) > 400:
        debug_log.pop(0)

# ── ESTADO GLOBAL ─────────────────────────────────────────────────────────────
# streams[ch] = {
#   "master_url"  : str,
#   "master_body" : str,   <- conteudo cacheado do master.m3u8
#   "hdntl"       : str,   <- valor do cookie hdntl Akamai
#   "ak_domain"   : str,   <- ex: spo4.akamaized.net
# }
# akamai_cookies = { domain: hdntl_value }
streams        = {}
akamai_cookies = {}
lock           = threading.Lock()

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _extract_m3u8(text):
    for pat in [
        r'https://[^\s"\'<>\\]+master\.m3u8[^\s"\'<>\\]*',
        r'https://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*',
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(0).replace("\\/", "/")
    return None


def _make_akamai_request(url, hdntl_val, stream=False):
    """Faz GET para o Akamai com o cookie hdntl correto no header."""
    headers = {
        "User-Agent": UA,
        "Referer":    BASE_URL + "/",
        "Origin":     BASE_URL,
    }
    # Envia hdntl como cookie no header HTTP diretamente (mais confiavel que Session)
    if hdntl_val:
        headers["Cookie"] = "hdntl=%s" % hdntl_val
    return requests.get(url, headers=headers, stream=stream, timeout=15)


def _hdntl_for_url(url):
    """Retorna o cookie hdntl correto para uma URL Akamai."""
    domain = urlparse(url).netloc
    with lock:
        # Busca exata
        if domain in akamai_cookies:
            return akamai_cookies[domain]
        # Busca parcial
        for d, v in akamai_cookies.items():
            if d in domain or domain in d:
                return v
    return None


# ── PLAYWRIGHT ────────────────────────────────────────────────────────────────
def _fetch_via_playwright():
    _dbg("[playwright] Iniciando…")
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    results        = {}
    new_ak_cookies = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
        )
        ctx  = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
        page = ctx.new_page()

        # Login
        _dbg("[playwright] GET login…")
        try:
            page.goto(BASE_URL + "/account/login",
                      wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            pass

        page.wait_for_timeout(4000)
        _dbg("[playwright] URL: " + page.url)
        page.fill("#UserName", EMAIL)
        page.wait_for_timeout(500)
        page.fill("#Password", PASSWORD)
        page.wait_for_timeout(500)

        _dbg("[playwright] Clicando Avancar…")
        try:
            page.click("button.main-button", timeout=10000)
        except Exception as e1:
            _dbg("[playwright] .main-button falhou: %s" % e1)
            try:
                page.locator("button[type=submit]").last.click(timeout=10000)
            except Exception as e2:
                _dbg("[playwright] submit JS fallback: %s" % e2)
                page.evaluate("document.querySelector('form').submit()")

        try:
            page.wait_for_url("**/escolhaseuperfil**", timeout=20000)
        except PWTimeout:
            _dbg("[playwright] URL apos submit: " + page.url)

        # Seleciona perfil
        if "escolhaseuperfil" in page.url.lower():
            _dbg("[playwright] Selecionando perfil…")
            try:
                page.click("img.profile-img[onclick*='%s']" % PROFILE_ID, timeout=5000)
            except Exception:
                try:
                    page.click("img.profile-img", timeout=5000)
                except Exception as e:
                    _dbg("[playwright] aviso perfil: %s" % e)
            try:
                page.wait_for_url("**/home**", timeout=15000)
            except PWTimeout:
                page.wait_for_timeout(3000)
            _dbg("[playwright] URL apos perfil: " + page.url)

        # Captura por canal
        for ch, info in CHANNELS.items():
            ch_url   = "%s/Live/LiveEvent/%s?groupId=%s" % (
                BASE_URL, info["event_id"], info["group_id"])
            captured = {}

            # Intercepta RESPOSTA do master.m3u8 para pegar conteudo + Set-Cookie
            def _on_resp(resp, _c=captured, _ch=ch):
                if "master.m3u8" in resp.url and not _c.get("master_url"):
                    try:
                        body = resp.body()
                        _c["master_url"]  = resp.url
                        _c["master_body"] = body.decode("utf-8", errors="replace")
                        sc = resp.all_headers().get("set-cookie", "")
                        m  = re.search(r"hdntl=([^;]+)", sc)
                        if m:
                            _c["hdntl"] = m.group(1)
                        _dbg("[playwright] [%s] master capturado %d bytes" % (
                            _ch.upper(), len(body)))
                    except Exception as ex:
                        _c["master_url"] = resp.url
                        _dbg("[playwright] [%s] body err: %s" % (_ch.upper(), ex))

            page.on("response", _on_resp)
            _dbg("[playwright] [%s] Navegando…" % ch.upper())
            try:
                page.goto(ch_url, wait_until="networkidle", timeout=25000)
            except PWTimeout:
                page.wait_for_timeout(3000)

            for _ in range(24):
                if captured.get("master_url"):
                    break
                page.wait_for_timeout(500)

            page.remove_listener("response", _on_resp)

            if not captured.get("master_url"):
                m3u8 = _extract_m3u8(page.content())
                if m3u8:
                    captured["master_url"] = m3u8

            if not captured.get("master_url"):
                _dbg("[playwright] [%s] FALHOU" % ch.upper())
                continue

            master_url = captured["master_url"]
            ak_domain  = urlparse(master_url).netloc

            # Pega hdntl: primeiro da resposta, depois dos cookies do contexto
            hdntl_val = captured.get("hdntl")
            if not hdntl_val:
                for c in ctx.cookies():
                    c_dom = c.get("domain", "").lstrip(".")
                    if c["name"] == "hdntl" and (
                        c_dom == ak_domain or
                        ak_domain.endswith("." + c_dom) or
                        c_dom.endswith("." + ak_domain) or
                        "akamai" in c_dom
                    ):
                        hdntl_val = c["value"]
                        _dbg("[playwright] [%s] hdntl via ctx (dom=%s)" % (
                            ch.upper(), c_dom))
                        break

            if hdntl_val:
                new_ak_cookies[ak_domain] = hdntl_val
                _dbg("[playwright] [%s] hdntl OK -> %s" % (ch.upper(), ak_domain))
            else:
                _dbg("[playwright] [%s] hdntl NAO encontrado!" % ch.upper())

            results[ch] = {
                "master_url":  master_url,
                "master_body": captured.get("master_body", ""),
                "hdntl":       hdntl_val or "",
                "ak_domain":   ak_domain,
            }

        browser.close()

    return results, new_ak_cookies


# ── LOOP DE RENOVACAO ─────────────────────────────────────────────────────────
def fetch_streams():
    _dbg("=" * 50)
    _dbg("CAPTURA INICIADA")
    _dbg("=" * 50)
    try:
        new, new_ak = _fetch_via_playwright()
        if new:
            with lock:
                streams.clear()
                streams.update(new)
                akamai_cookies.clear()
                akamai_cookies.update(new_ak)
            _dbg("Streams: %s" % list(new.keys()))
            _dbg("Cookies Akamai: %s" % list(new_ak.keys()))
        else:
            _dbg("Nenhum stream capturado")
    except Exception:
        _dbg("ERRO:\n" + traceback.format_exc())


def _refresh_loop():
    while True:
        try:
            fetch_streams()
        except Exception:
            _dbg("ERRO CRITICO:\n" + traceback.format_exc())
        _dbg("Proxima renovacao em %d min" % (REFRESH_INTERVAL // 60))
        time.sleep(REFRESH_INTERVAL)


# ── REESCRITA M3U8 ────────────────────────────────────────────────────────────
def _proxy_url(url):
    return "/proxy?u=%s" % quote(url, safe="")


def _rewrite_m3u8(content, base_url):
    parsed    = urlparse(base_url)
    base_root = "%s://%s" % (parsed.scheme, parsed.netloc)
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


# ── FLASK ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    host = req.host.split(":")[0]
    rows = "".join(
        "<tr><td><b>%s</b></td><td>%s</td>"
        "<td><code>http://%s:%d/channel/%s</code></td>"
        "<td>%s</td></tr>" % (
            ch.upper(), CHANNELS[ch]["name"], host, PORT, ch,
            "OK" if ch in streams else "Aguardando"
        )
        for ch in CHANNELS
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>RecordPlus Proxy</title></head>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>RecordPlus HLS Proxy v3</h2>"
        "<table border='1' cellpadding='8'>"
        "<tr><th>ID</th><th>Canal</th><th>URL VLC</th><th>Status</th></tr>"
        + rows +
        "</table><p><a href='/debug'>Log de diagnostico</a></p>"
        "</body></html>"
    )


@app.route("/debug")
def debug():
    with lock:
        info = {ch: {
            "url":    streams[ch].get("master_url", "")[:80],
            "domain": streams[ch].get("ak_domain", ""),
            "hdntl":  "OK" if streams[ch].get("hdntl") else "MISSING",
            "body_cached": bool(streams[ch].get("master_body")),
        } for ch in streams}
    lines = "\n".join(debug_log[-150:])
    return Response(
        "STREAMS:\n%s\n\nAKAMAI COOKIES:\n%s\n\n%s\nLOG:\n%s" % (
            info, list(akamai_cookies.keys()), "=" * 50, lines),
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/channel/<ch>")
def channel(ch):
    if ch not in CHANNELS:
        abort(404)
    with lock:
        info = streams.get(ch)
    if not info:
        return Response(
            "Stream nao disponivel ainda. Aguarde ~60s e tente novamente.\n"
            "Diagnostico: http://%s/debug" % req.host,
            status=503, mimetype="text/plain")

    master_body = info.get("master_body", "")
    master_url  = info["master_url"]
    hdntl_val   = info.get("hdntl", "")

    # Se temos o conteudo cacheado, servimos direto (sem re-fetch ao Akamai)
    if master_body:
        _dbg("[%s] Servindo master cacheado (%d bytes)" % (ch, len(master_body)))
        content = _rewrite_m3u8(master_body, master_url)
        return Response(content, mimetype="application/x-mpegURL",
                        headers={"Cache-Control": "no-cache, no-store"})

    # Fallback: busca do Akamai com o cookie correto
    _dbg("[%s] Buscando master do Akamai…" % ch)
    try:
        r = _make_akamai_request(master_url, hdntl_val)
        r.raise_for_status()
    except Exception as e:
        _dbg("[%s] Erro upstream: %s" % (ch, e))
        abort(502)

    content = _rewrite_m3u8(r.text, master_url)
    return Response(content, mimetype="application/x-mpegURL",
                    headers={"Cache-Control": "no-cache, no-store"})


@app.route("/proxy")
def proxy():
    url = unquote(req.args.get("u", ""))
    if not url:
        abort(400)

    hdntl_val = _hdntl_for_url(url)
    if not hdntl_val:
        # Tenta qualquer cookie disponivel
        with lock:
            vals = list(akamai_cookies.values())
        hdntl_val = vals[0] if vals else None

    try:
        up = _make_akamai_request(url, hdntl_val, stream=True)
        up.raise_for_status()
    except Exception as e:
        _dbg("proxy err: %s -> %s" % (e, url[:60]), "warning")
        abort(502)

    ct = up.headers.get("Content-Type", "application/octet-stream")

    if "mpegURL" in ct or url.split("?")[0].endswith(".m3u8"):
        content = _rewrite_m3u8(up.text, url)
        return Response(content, mimetype="application/x-mpegURL",
                        headers={"Cache-Control": "no-cache"})

    def _stream():
        for chunk in up.iter_content(65536):
            if chunk:
                yield chunk

    return Response(_stream(), mimetype=ct,
                    headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"})


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50, flush=True)
    print("  RecordPlus Proxy v3  |  porta %d" % PORT, flush=True)
    print("=" * 50, flush=True)

    t = threading.Thread(target=_refresh_loop, daemon=True, name="refresh")
    t.start()

    print("Aguardando captura inicial (max 120s)…", flush=True)
    for _ in range(240):
        time.sleep(0.5)
        with lock:
            if streams:
                break

    print("Streams: %s" % list(streams.keys()), flush=True)
    print("Iniciando Flask em 0.0.0.0:%d" % PORT, flush=True)
    sys.stdout.flush()

    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
