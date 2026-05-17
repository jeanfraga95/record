#!/usr/bin/env python3
"""
RecordPlus HLS Proxy  v4.0
===========================
Canais Akamai (SP, Rio, Minas): proxy M3U8 padrão (token via cookie hdntl)
Canais cdnsimba: RELAY TS em tempo real — o proxy baixa os segmentos
                 internamente renovando o bpk-token a cada ciclo de playlist
                 e retransmite bytes brutos ao VLC como video/MP2T.
                 O VLC recebe um fluxo contínuo sem nunca ver URLs com token.

Links fixos:
  http://<IP>:8888/channel/<canal>
  http://<IP>:8888/channel/<canal>/<qualidade>   (fhd | hd | sd)

Painel: http://<IP>:8888/
Debug:  http://<IP>:8888/debug
"""

import re
import sys
import time
import threading
import logging
import traceback
import collections
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, Response, request as req, abort

# ── CONFIG ────────────────────────────────────────────────────────────────────
EMAIL      = "jean.fraga20@gmail.com"
PASSWORD   = "qwerty123"
PROFILE_ID = "8a7ea0f8-c8c1-4a29-b424-14bbf7ee9275"

CHANNELS = {
    "sp":                 {"name": "Record SP",                    "event_id": "180", "group_id": "7"},
    "rio":                {"name": "Record Rio",                   "event_id": "182", "group_id": "7"},
    "minas":              {"name": "Record Minas",                 "event_id": "186", "group_id": "7"},
    "guaiba":             {"name": "Record Guaiba",                "event_id": "178", "group_id": "7"},
    "bahia":              {"name": "Record Bahia",                 "event_id": "187", "group_id": "7"},
    "brasilia":           {"name": "Record Brasilia",              "event_id": "185", "group_id": "7"},
    "goias":              {"name": "Record Goias",                 "event_id": "189", "group_id": "7"},
    "belem":              {"name": "Record Belem",                 "event_id": "188", "group_id": "7"},
    "manaus":             {"name": "Record Manaus",                "event_id": "249", "group_id": "7"},
    "santos_vale":        {"name": "Record Santos e Vale",         "event_id": "597", "group_id": "7"},
    "bahia_itabuna":      {"name": "Record Bahia Itabuna",         "event_id": "598", "group_id": "7"},
    "bauru":              {"name": "Record Bauru",                 "event_id": "599", "group_id": "7"},
    "rio_preto":          {"name": "Record Rio Preto",             "event_id": "600", "group_id": "7"},
    "ribeirao_preto":     {"name": "Record Ribeirao Preto",        "event_id": "601", "group_id": "7"},
    "campos_goytacazes":  {"name": "Record Campos dos Goytacazes", "event_id": "602", "group_id": "7"},
    "Nsports":            {"name": "Nsports",                      "event_id": "644", "group_id": "76"},
    "Manual_do_Mundo":    {"name": "Manual do Mundo",              "event_id": "630", "group_id": "65"},
    "Acelerados":         {"name": "Acelerados",                   "event_id": "625", "group_id": "63"},
    "good_game_tv":       {"name": "Good Game TV",                 "event_id": "629", "group_id": "66"},
    "Desimpedidos":       {"name": "Desimpedidos",                 "event_id": "627", "group_id": "64"},
    "canal_do_artesanato":{"name": "Canal do Artesanato",          "event_id": "626", "group_id": "68"},
    "Record_news":        {"name": "Record News",                  "event_id": "191", "group_id": "10"},
}

PORT               = 8888
REFRESH_INTERVAL   = 1500   # 25 min — renovação completa via Playwright
ORIGIN_LOOP_INTERVAL = 180  # 3 min — re-captura origin_url dos canais cdnsimba
BASE_URL           = "https://www.recordplus.com"

PANEL_USER     = "admin"
PANEL_PASSWORD = "admin123"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── LOGGING ───────────────────────────────────────────────────────────────────
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

debug_log = collections.deque(maxlen=500)

def _dbg(msg, level="info"):
    getattr(log, level)(msg)
    sys.stdout.flush()
    debug_log.append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

# ── ESTADO GLOBAL ─────────────────────────────────────────────────────────────
streams        = {}          # ch -> {master_url, master_body, hdntl, ak_domain, variants, origin_url}
akamai_cookies = {}          # domain -> hdntl value
lock           = threading.Lock()
_renewing      = False

# Cache bpk-token: origin_url -> {cache_base, token, ts}
_simba_token_cache = {}
_simba_lock        = threading.Lock()

# Recapturas urgentes em andamento (evita paralelas)
_simba_urgent_running = set()
_simba_urgent_lock    = threading.Lock()

_session_cookies = []

# Contadores de clientes relay ativos por canal
_relay_clients = collections.defaultdict(int)
_relay_lock    = threading.Lock()

# ── HELPERS ───────────────────────────────────────────────────────────────────
_HEADERS = {"User-Agent": UA, "Referer": BASE_URL + "/", "Origin": BASE_URL}


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
    h = dict(_HEADERS)
    if hdntl_val:
        h["Cookie"] = "hdntl=%s" % hdntl_val
    return requests.get(url, headers=h, stream=stream, timeout=15)


def _hdntl_for_url(url):
    domain = urlparse(url).netloc
    with lock:
        if domain in akamai_cookies:
            return akamai_cookies[domain]
        for d, v in akamai_cookies.items():
            if d in domain or domain in d:
                return v
    return None


def _is_simba(info):
    return "cdnsimba" in info.get("origin_url", "")


def _trigger_refresh():
    """Renovação completa de todos os canais via Playwright."""
    global _renewing
    with lock:
        if _renewing:
            return
        _renewing = True

    def _run():
        global _renewing
        _dbg("⚡ Renovacao de emergencia iniciada!")
        try:
            fetch_streams()
        except Exception:
            _dbg("ERRO na renovacao de emergencia:\n" + traceback.format_exc())
        finally:
            global _renewing
            _renewing = False
            _dbg("⚡ Renovacao de emergencia concluida.")

    threading.Thread(target=_run, daemon=True, name="emergency-refresh").start()


def _trigger_simba_refresh_ch(ch):
    """Recaptura urgente de origin_url apenas para o canal ch (usa cookies salvos)."""
    global _session_cookies

    with _simba_urgent_lock:
        if ch in _simba_urgent_running:
            return
        _simba_urgent_running.add(ch)

    def _run():
        try:
            if not _session_cookies:
                _dbg("[simba][%s] sem cookies de sessao, ignorando" % ch.upper())
                return

            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
            _dbg("[simba][%s] ⚡ Recaptura urgente de origin_url" % ch.upper())

            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox",
                          "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
                )
                ctx  = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
                ctx.add_cookies(_session_cookies)
                page = ctx.new_page()

                ch_url   = "%s/Live/LiveEvent/%s?groupId=%s" % (
                    BASE_URL, CHANNELS[ch]["event_id"], CHANNELS[ch]["group_id"])
                captured = {}

                def _on_req(request, _c=captured):
                    url = request.url
                    if "cdnsimba" in url and \
                       ("index.m3u8" in url or "master.m3u8" in url) and \
                       not _c.get("origin_url"):
                        _c["origin_url"] = url

                page.on("request", _on_req)
                try:
                    page.goto(ch_url, wait_until="domcontentloaded", timeout=20000)
                except PWTimeout:
                    pass
                for _ in range(16):
                    if captured.get("origin_url"):
                        break
                    page.wait_for_timeout(500)
                page.remove_listener("request", _on_req)
                browser.close()

            if captured.get("origin_url"):
                new_origin = captured["origin_url"]
                with lock:
                    if ch in streams:
                        streams[ch]["origin_url"] = new_origin
                # Invalida token cache antigo
                with _simba_lock:
                    _simba_token_cache.pop(new_origin, None)
                _dbg("[simba][%s] ⚡ origin_url atualizada → %s…" % (ch.upper(), new_origin[:60]))
            else:
                _dbg("[simba][%s] ⚡ origin_url nao capturada — sessao expirada?" % ch.upper())
                _trigger_refresh()

        except Exception:
            _dbg("[simba][%s] ERRO na recaptura urgente:\n" % ch.upper() + traceback.format_exc())
        finally:
            with _simba_urgent_lock:
                _simba_urgent_running.discard(ch)

    threading.Thread(target=_run, daemon=True, name="simba-urgent-%s" % ch).start()


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

        global _session_cookies
        _session_cookies = ctx.cookies()
        _dbg("[playwright] %d cookies salvos" % len(_session_cookies))

        for ch, info in CHANNELS.items():
            ch_url   = "%s/Live/LiveEvent/%s?groupId=%s" % (
                BASE_URL, info["event_id"], info["group_id"])
            captured = {}

            def _on_req(request, _c=captured, _ch=ch):
                url = request.url
                if "cdnsimba" in url and \
                   ("index.m3u8" in url or "master.m3u8" in url) and \
                   not _c.get("simba_origin_url"):
                    _c["simba_origin_url"] = url
                    _dbg("[playwright][%s] origin_url capturada" % _ch.upper())

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
                        _dbg("[playwright][%s] master %d bytes" % (_ch.upper(), len(body)))
                    except Exception:
                        _c["master_url"]  = resp.url
                        _c["needs_fetch"] = True

            page.on("request", _on_req)
            page.on("response", _on_resp)
            _dbg("[playwright][%s] Navegando…" % ch.upper())
            try:
                page.goto(ch_url, wait_until="networkidle", timeout=25000)
            except PWTimeout:
                page.wait_for_timeout(3000)
            for _ in range(24):
                if captured.get("master_url"):
                    break
                page.wait_for_timeout(500)
            page.remove_listener("response", _on_resp)
            page.remove_listener("request", _on_req)

            if not captured.get("master_url"):
                m3u8 = _extract_m3u8(page.content())
                if m3u8:
                    captured["master_url"] = m3u8
                    captured["needs_fetch"] = True

            if not captured.get("master_url"):
                _dbg("[playwright][%s] FALHOU" % ch.upper())
                continue

            master_url = captured["master_url"]
            ak_domain  = urlparse(master_url).netloc

            if captured.get("needs_fetch") and not captured.get("master_body"):
                try:
                    r = requests.get(master_url, headers=_HEADERS, timeout=10,
                                     allow_redirects=True)
                    r.raise_for_status()
                    captured["master_body"] = r.text
                    if r.url != master_url:
                        master_url = r.url
                        captured["master_url"] = master_url
                        ak_domain = urlparse(master_url).netloc
                    _dbg("[playwright][%s] body via requests OK" % ch.upper())
                except Exception as ex:
                    _dbg("[playwright][%s] requests fallback falhou: %s" % (ch.upper(), ex))

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
                        break

            if hdntl_val:
                new_ak_cookies[ak_domain] = hdntl_val

            master_body = captured.get("master_body", "")
            variants    = _parse_variants(master_body, master_url) if master_body else {}
            origin_url  = captured.get("simba_origin_url") or captured.get("origin_url", "")

            results[ch] = {
                "master_url":  master_url,
                "master_body": master_body,
                "hdntl":       hdntl_val or "",
                "ak_domain":   ak_domain,
                "variants":    variants,
                "origin_url":  origin_url,
            }
            _dbg("[playwright][%s] OK | cdn=%s | variantes=%s" % (
                ch.upper(),
                "cdnsimba" if "cdnsimba" in origin_url else "akamai",
                list(variants.keys()),
            ))

        browser.close()

    return results, new_ak_cookies


# ── LOOPS DE RENOVAÇÃO ────────────────────────────────────────────────────────
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
        else:
            _dbg("Nenhum stream capturado")
    except Exception:
        _dbg("ERRO:\n" + traceback.format_exc())


def _recapture_simba_origins():
    """Re-captura origin_url dos canais cdnsimba usando cookies salvos (sem novo login)."""
    global _session_cookies
    if not _session_cookies:
        return

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with lock:
        simba_chs = {
            ch: info for ch, info in streams.items()
            if "cdnsimba" in info.get("origin_url", "")
        }
    if not simba_chs:
        return

    _dbg("[simba] Re-capturando origin_url para: %s" % list(simba_chs.keys()))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
            )
            ctx  = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
            ctx.add_cookies(_session_cookies)
            page = ctx.new_page()

            for ch, info in simba_chs.items():
                ch_url   = "%s/Live/LiveEvent/%s?groupId=%s" % (
                    BASE_URL, CHANNELS[ch]["event_id"], CHANNELS[ch]["group_id"])
                captured = {}

                def _on_req(request, _c=captured, _ch=ch):
                    url = request.url
                    if "cdnsimba" in url and \
                       ("index.m3u8" in url or "master.m3u8" in url) and \
                       not _c.get("origin_url"):
                        _c["origin_url"] = url
                        _dbg("[simba][%s] origin_url renovada" % _ch.upper())

                page.on("request", _on_req)
                try:
                    page.goto(ch_url, wait_until="domcontentloaded", timeout=15000)
                except PWTimeout:
                    pass
                for _ in range(12):
                    if captured.get("origin_url"):
                        break
                    page.wait_for_timeout(500)
                page.remove_listener("request", _on_req)

                if captured.get("origin_url"):
                    new_origin = captured["origin_url"]
                    with lock:
                        if ch in streams:
                            streams[ch]["origin_url"] = new_origin
                    with _simba_lock:
                        _simba_token_cache.pop(new_origin, None)
                else:
                    _dbg("[simba][%s] origin_url NAO capturada" % ch.upper())

            browser.close()
    except Exception:
        _dbg("[simba] ERRO na recaptura:\n" + traceback.format_exc())


def _origin_loop():
    """A cada ORIGIN_LOOP_INTERVAL renova as origin_url dos canais cdnsimba."""
    while True:
        time.sleep(ORIGIN_LOOP_INTERVAL)
        try:
            _recapture_simba_origins()
        except Exception:
            _dbg("[simba] ERRO no loop:\n" + traceback.format_exc())


def _refresh_loop():
    while True:
        try:
            fetch_streams()
        except Exception:
            _dbg("ERRO CRITICO:\n" + traceback.format_exc())
        _dbg("Proxima renovacao completa em %d min" % (REFRESH_INTERVAL // 60))
        time.sleep(REFRESH_INTERVAL)


# ── REESCRITA M3U8 (Akamai) ───────────────────────────────────────────────────
def _proxy_url(url, ch=""):
    base = "/proxy?u=%s" % quote(url, safe="")
    if ch:
        base += "&_ch=%s" % quote(ch, safe="")
    return base


def _abs_url(url, base_root):
    if url.startswith("http"):
        return url
    return base_root + "/" + url.lstrip("/")


def _rewrite_m3u8(content, base_url, ch=""):
    parsed    = urlparse(base_url)
    base_root = "%s://%s" % (parsed.scheme, parsed.netloc)
    out = []

    def _rewrite_uri_attr(line):
        def _replace(m):
            original = m.group(1)
            abs_u    = _abs_url(original, base_root)
            return 'URI="%s"' % _proxy_url(abs_u, ch)
        return re.sub(r'URI="([^"]+)"', _replace, line)

    for line in content.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            if 'URI="' in s:
                out.append(_rewrite_uri_attr(line))
            else:
                out.append(line)
        elif s.startswith("http"):
            out.append(_proxy_url(s, ch))
        else:
            out.append(_proxy_url(base_root + "/" + s.lstrip("/"), ch))

    return "\n".join(out)


def _parse_variants(content, base_url):
    parsed    = urlparse(base_url)
    base_root = "%s://%s" % (parsed.scheme, parsed.netloc)
    variants  = {}
    lines     = content.splitlines()

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        for j in range(i + 1, len(lines)):
            url_line = lines[j].strip()
            if not url_line or url_line.startswith("#"):
                continue
            abs_url = url_line if url_line.startswith("http") else base_root + "/" + url_line.lstrip("/")
            res = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if res:
                h = int(res.group(1))
                if h >= 1080:
                    variants["fhd"] = abs_url
                elif h >= 720:
                    variants["hd"] = abs_url
                elif h >= 480:
                    variants.setdefault("sd", abs_url)
                else:
                    variants.setdefault("low", abs_url)
            break

    if "fhd" not in variants and "hd" in variants:
        variants["fhd"] = variants["hd"]
    if "hd" not in variants and "fhd" in variants:
        variants["hd"] = variants["fhd"]
    if "sd" not in variants:
        variants["sd"] = variants.get("low") or variants.get("hd") or variants.get("fhd", "")

    return {k: v for k, v in variants.items() if k in ("fhd", "hd", "sd") and v}


# ── TOKEN CDNSIMBA ────────────────────────────────────────────────────────────
def _get_fresh_simba_token(origin_url):
    """
    GET na origin_url (sem seguir redirect), extrai bpk-token do Location.
    Retorna (cache_base, token) ou (None, None).
    Cache de 50s para evitar requests desnecessários.
    """
    with _simba_lock:
        cached = _simba_token_cache.get(origin_url)
        if cached and (time.time() - cached["ts"]) < 50:
            return cached["cache_base"], cached["token"]

    try:
        r = requests.get(
            origin_url,
            headers=_HEADERS,
            timeout=10,
            allow_redirects=False,
        )
        location = r.headers.get("Location", "")
        # Exemplo: https://cache01.cdnsimba.com.br:443/bpk-token/TOKEN/path
        m = re.match(r"(https://[^/]+)/bpk-token/([^/]+)/", location)
        if not m:
            _dbg("[simba] Location invalida: '%s'" % location[:80], "warning")
            return None, None

        cache_base  = m.group(1)
        fresh_token = m.group(2)
        with _simba_lock:
            _simba_token_cache[origin_url] = {
                "cache_base": cache_base,
                "token":      fresh_token,
                "ts":         time.time(),
            }
        return cache_base, fresh_token

    except Exception as e:
        _dbg("[simba] erro ao buscar token: %s" % e, "warning")
        return None, None


def _simba_url_with_fresh_token(url, origin_url):
    """Substitui o bpk-token em 'url' por um token fresco."""
    cache_base, fresh_token = _get_fresh_simba_token(origin_url)
    if not cache_base or not fresh_token:
        return url   # fallback: URL original (pode falhar)
    m = re.search(r"/bpk-token/[^/]+(/.*)", url)
    if not m:
        return url
    return "%s/bpk-token/%s%s" % (cache_base, fresh_token, m.group(1))


def _fetch_master_live(info):
    """Re-busca o master.m3u8 com token fresco."""
    origin_url = info.get("origin_url", "")
    if origin_url and "cdnsimba" in origin_url:
        cache_base, fresh_token = _get_fresh_simba_token(origin_url)
        if cache_base and fresh_token:
            m = re.search(r"/bpk-token/[^/]+(/.*)", info["master_url"])
            fresh_url = ("%s/bpk-token/%s%s" % (cache_base, fresh_token, m.group(1))
                         if m else info["master_url"])
        else:
            fresh_url = info["master_url"]
        r = requests.get(fresh_url, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        return r.text, fresh_url
    else:
        r = _make_akamai_request(info["master_url"], info.get("hdntl", ""))
        r.raise_for_status()
        return r.text, info["master_url"]


# ── RELAY TS (cdnsimba) ───────────────────────────────────────────────────────
def _simba_relay_generator(ch, quality=None):
    """
    Gerador de relay TS para canais cdnsimba.

    Fluxo:
      1. Obtém variante HLS (fhd > hd > sd) com token fresco
      2. Faz polling da playlist de variante a cada ~2s
      3. Detecta segmentos novos pelo número de sequência
      4. Renova o bpk-token em cada URL de segmento antes de baixar
      5. Envia bytes brutos ao cliente (VLC recebe MPEG-TS contínuo)
      6. Em caso de falha repetida, dispara recaptura de origin_url
         e reinicia do step 1 com a nova origin automaticamente

    O cliente nunca precisa lidar com tokens ou reescritas de URL.
    """
    seen_segs   = collections.deque(maxlen=200)  # guarda até 200 URLs já enviadas
    seen_set    = set()
    last_seq    = -1
    variant_url = None
    err_count   = 0
    MAX_ERRS    = 4

    _dbg("[relay][%s] cliente conectado (qualidade=%s)" % (ch, quality or "auto"))
    with _relay_lock:
        _relay_clients[ch] += 1

    try:
        while True:
            # ── Lê estado atual do canal ──────────────────────────────────
            with lock:
                info = streams.get(ch)
            if not info:
                time.sleep(5)
                continue

            origin_url = info.get("origin_url", "")
            if not origin_url or "cdnsimba" not in origin_url:
                time.sleep(5)
                continue

            # ── Obtém/renova URL da variante ──────────────────────────────
            if not variant_url:
                try:
                    body, live_url = _fetch_master_live(info)
                    variants = _parse_variants(body, live_url)
                    if quality and quality in variants:
                        variant_url = variants[quality]
                    else:
                        variant_url = (variants.get("fhd") or
                                       variants.get("hd") or
                                       variants.get("sd"))
                    if not variant_url:
                        _dbg("[relay][%s] nenhuma variante disponivel" % ch)
                        time.sleep(5)
                        continue
                    _dbg("[relay][%s] variante: %s…" % (ch, variant_url[:70]))
                except Exception as e:
                    _dbg("[relay][%s] erro ao buscar master: %s" % (ch, e))
                    err_count += 1
                    if err_count >= MAX_ERRS:
                        _dbg("[relay][%s] muitos erros, recapturando origin" % ch)
                        _trigger_simba_refresh_ch(ch)
                        err_count = 0
                        # Aguarda recaptura completar (até 15s)
                        for _ in range(15):
                            time.sleep(1)
                            with lock:
                                new_info = streams.get(ch, {})
                            if new_info.get("origin_url", "") != origin_url:
                                break
                    time.sleep(3)
                    continue

            # ── Busca playlist de variante com token fresco ───────────────
            try:
                fresh_variant = _simba_url_with_fresh_token(variant_url, origin_url)
                r = requests.get(fresh_variant, headers=_HEADERS, timeout=10)
                r.raise_for_status()
                playlist_text = r.text
                playlist_base = fresh_variant.rsplit("/", 1)[0]
            except Exception as e:
                _dbg("[relay][%s] erro ao buscar playlist: %s" % (ch, e))
                err_count += 1
                if err_count >= MAX_ERRS:
                    with _simba_lock:
                        _simba_token_cache.pop(origin_url, None)
                    _trigger_simba_refresh_ch(ch)
                    err_count   = 0
                    variant_url = None
                time.sleep(3)
                continue

            # ── Parse de segmentos ────────────────────────────────────────
            cur_seq  = 0
            segments = []
            for line in playlist_text.splitlines():
                s = line.strip()
                if s.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    try:
                        cur_seq = int(s.split(":", 1)[1])
                    except ValueError:
                        pass
                elif s and not s.startswith("#"):
                    seg_url = s if s.startswith("http") else playlist_base + "/" + s.lstrip("/")
                    segments.append((cur_seq, seg_url))
                    cur_seq += 1

            novos = [
                (seq, url) for seq, url in segments
                if seq > last_seq and url not in seen_set
            ]

            if not novos:
                time.sleep(1.5)
                continue

            # ── Baixa e retransmite cada segmento novo ────────────────────
            for seq, seg_url in novos:
                fresh_seg = _simba_url_with_fresh_token(seg_url, origin_url)
                try:
                    sr = requests.get(fresh_seg, headers=_HEADERS, timeout=20, stream=True)
                    sr.raise_for_status()
                    for chunk in sr.iter_content(65536):
                        if chunk:
                            yield chunk

                    # Marcado como enviado
                    if seg_url in seen_set:
                        pass
                    else:
                        old = seen_segs.appendleft(seg_url)  # appendleft para FIFO com deque
                        seen_set.add(seg_url)
                        # Limpa URLs mais antigas do set quando deque encher
                        if len(seen_segs) >= 200:
                            removed = seen_segs.pop()
                            seen_set.discard(removed)

                    last_seq  = seq
                    err_count = 0

                except Exception as e:
                    _dbg("[relay][%s] erro no segmento seq=%d: %s" % (ch, seq, e))
                    err_count += 1
                    if err_count >= MAX_ERRS:
                        _dbg("[relay][%s] token provavelmente expirado — forçando refresh" % ch)
                        with _simba_lock:
                            _simba_token_cache.pop(origin_url, None)
                        _trigger_simba_refresh_ch(ch)
                        err_count   = 0
                        variant_url = None
                    break   # Volta ao início do loop para obter variante fresca

            time.sleep(0.3)

    except GeneratorExit:
        _dbg("[relay][%s] cliente desconectou" % ch)
    except Exception as e:
        _dbg("[relay][%s] ERRO fatal no relay:\n%s" % (ch, traceback.format_exc()))
    finally:
        with _relay_lock:
            _relay_clients[ch] = max(0, _relay_clients[ch] - 1)
        _dbg("[relay][%s] gerador encerrado" % ch)


# ── FLASK ─────────────────────────────────────────────────────────────────────
import secrets
from functools import wraps
from flask import session, redirect

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)


def _check_auth(username, password):
    return (secrets.compare_digest(username, PANEL_USER) and
            secrets.compare_digest(password, PANEL_PASSWORD))


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>RecordPlus Proxy — Login</title>
<style>
* {{ box-sizing:border-box;margin:0;padding:0; }}
body {{ background:#1a1a2e;display:flex;justify-content:center;
       align-items:center;min-height:100vh;font-family:sans-serif; }}
.card {{ background:#16213e;border-radius:12px;padding:40px 36px;
        width:340px;box-shadow:0 8px 32px rgba(0,0,0,.4); }}
h2 {{ color:#e0e0e0;text-align:center;margin-bottom:8px;font-size:20px; }}
p.sub {{ color:#888;text-align:center;font-size:13px;margin-bottom:28px; }}
label {{ color:#aaa;font-size:13px;display:block;margin-bottom:6px; }}
input {{ width:100%;padding:10px 14px;border-radius:6px;border:1px solid #2a4080;
        background:#0f3460;color:#e0e0e0;font-size:14px;margin-bottom:18px;outline:none; }}
input:focus {{ border-color:#e53935; }}
button {{ width:100%;padding:11px;background:#e53935;color:#fff;border:none;
         border-radius:6px;font-size:15px;font-weight:bold;cursor:pointer; }}
button:hover {{ background:#c62828; }}
.error {{ background:#4a0000;color:#ff8a80;border-radius:6px;
         padding:10px 14px;font-size:13px;margin-bottom:16px;text-align:center; }}
</style></head>
<body><div class='card'>
  <h2>&#128250; RecordPlus Proxy</h2>
  <p class='sub'>Acesso restrito</p>
  {error}
  <form method='POST' action='/login'>
    <label>Usuário</label>
    <input type='text' name='username' autofocus autocomplete='username'>
    <label>Senha</label>
    <input type='password' name='password' autocomplete='current-password'>
    <button type='submit'>Entrar</button>
  </form>
</div></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if req.method == "POST":
        u = req.form.get("username", "")
        p = req.form.get("password", "")
        if _check_auth(u, p):
            session["logged_in"] = True
            return redirect("/")
        return LOGIN_HTML.format(error="<div class='error'>Usuário ou senha incorretos.</div>"), 401
    return LOGIN_HTML.format(error="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@_login_required
def index():
    host = req.host.split(":")[0]
    QUALITY_LABEL = {"fhd": "Full HD 1080p", "hd": "HD 720p", "sd": "SD 480p"}
    rows = []
    for ch in CHANNELS:
        with lock:
            info = streams.get(ch, {})
        cdn    = "cdnsimba" if "cdnsimba" in info.get("origin_url", "") else "akamai"
        status = "OK [%s]" % cdn if ch in streams else "Aguardando"
        with _relay_lock:
            clients = _relay_clients.get(ch, 0)
        relay_info = " | %d cliente(s)" % clients if cdn == "cdnsimba" and clients > 0 else ""
        variants = info.get("variants", {})

        rows.append(
            "<tr>"
            "<td rowspan='4'><b>%s</b><br><small>%s</small></td>"
            "<td>Auto</td>"
            "<td><code>http://%s:%d/channel/%s</code></td>"
            "<td>%s%s</td>"
            "</tr>" % (ch.upper(), CHANNELS[ch]["name"], host, PORT, ch, status, relay_info)
        )
        for q in ("fhd", "hd", "sd"):
            q_status = "OK" if q in variants else "N/A"
            rows.append(
                "<tr><td>%s</td><td><code>http://%s:%d/channel/%s/%s</code></td>"
                "<td>%s</td></tr>" % (QUALITY_LABEL[q], host, PORT, ch, q, q_status)
            )

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>RecordPlus Proxy</title></head>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>RecordPlus HLS Proxy v4.0</h2>"
        "<p>Canais cdnsimba usam <b>relay TS</b> — fluxo contínuo sem expiração de token.</p>"
        "<p style='margin-top:8px'>Use os links no VLC: <b>Media → Abrir fluxo de rede</b></p>"
        "<table border='1' cellpadding='8' cellspacing='0' style='margin-top:1em'>"
        "<tr><th>Canal</th><th>Qualidade</th><th>URL para VLC</th><th>Status</th></tr>"
        + "".join(rows) +
        "</table><br>"
        "<a href='/playlist.m3u' download='recordplus.m3u'>"
        "<button style='margin-right:12px;padding:8px 18px;font-size:14px;"
        "background:#e53935;color:#fff;border:none;border-radius:4px;cursor:pointer'>"
        "⬇ Baixar playlist M3U</button></a>"
        "<a href='/debug' style='font-size:14px'>Log de diagnóstico</a>"
        " | <a href='/logout' style='font-size:14px'>Sair</a>"
        "</body></html>"
    )


@app.route("/playlist.m3u")
@_login_required
def playlist_m3u():
    host = req.host.split(":")[0]
    QUALITY_LABEL = {"fhd": "Full HD 1080p", "hd": "HD 720p", "sd": "SD 480p"}
    lines = ["#EXTM3U"]
    with lock:
        snap = dict(streams)
    for ch, info in snap.items():
        ch_name  = CHANNELS[ch]["name"]
        variants = info.get("variants", {})
        lines.append(
            '#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="RecordPlus",%s' % (
                ch, ch_name, ch_name))
        lines.append("http://%s:%d/channel/%s" % (host, PORT, ch))
        for q in ("fhd", "hd", "sd"):
            if q not in variants:
                continue
            label = "%s (%s)" % (ch_name, QUALITY_LABEL[q])
            lines.append(
                '#EXTINF:-1 tvg-id="%s_%s" tvg-name="%s" group-title="RecordPlus %s",%s' % (
                    ch, q, label, QUALITY_LABEL[q], label))
            lines.append("http://%s:%d/channel/%s/%s" % (host, PORT, ch, q))
    content = "\n".join(lines) + "\n"
    return Response(
        content,
        mimetype="audio/x-mpegurl",
        headers={"Content-Disposition": "attachment; filename=recordplus.m3u"},
    )


@app.route("/debug")
@_login_required
def debug():
    with lock:
        stream_info = {}
        for ch in streams:
            info = streams[ch]
            with _relay_lock:
                clients = _relay_clients.get(ch, 0)
            stream_info[ch] = {
                "cdn":         "cdnsimba" if "cdnsimba" in info.get("origin_url", "") else "akamai",
                "mode":        "relay-TS" if "cdnsimba" in info.get("origin_url", "") else "m3u8-proxy",
                "relay_clnts": clients,
                "origin_url":  info.get("origin_url", "N/A")[:70],
                "master_url":  info.get("master_url", "")[:70],
                "hdntl":       "OK" if info.get("hdntl") else "MISSING",
                "variantes":   list(info.get("variants", {}).keys()),
            }
    with _simba_lock:
        token_cache = {
            k[:60]: "age=%.0fs  token=%s…" % (
                time.time() - v["ts"], v["token"][:12])
            for k, v in _simba_token_cache.items()
        }
    with _relay_lock:
        relay_active = dict(_relay_clients)

    lines = "\n".join(list(debug_log)[-150:])
    import json
    return Response(
        "STREAMS:\n%s\n\nRELAY CLIENTES ATIVOS:\n%s\n\nSIMBA TOKEN CACHE:\n%s\n\n"
        "AKAMAI COOKIES:\n%s\n\n%s\nLOG:\n%s" % (
            json.dumps(stream_info, indent=2, ensure_ascii=False),
            json.dumps(relay_active, indent=2),
            json.dumps(token_cache, indent=2, ensure_ascii=False),
            list(akamai_cookies.keys()),
            "=" * 50,
            lines,
        ),
        mimetype="text/plain; charset=utf-8",
    )


# ── ROTAS DE CANAL ────────────────────────────────────────────────────────────
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

    if _is_simba(info):
        # ── Modo relay TS para cdnsimba ──
        return Response(
            _simba_relay_generator(ch),
            mimetype="video/MP2T",
            headers={
                "Cache-Control":    "no-cache, no-store",
                "X-Accel-Buffering":"no",
                "Connection":       "keep-alive",
            },
        )

    # ── Modo M3U8 proxy para Akamai ──
    _dbg("[%s] Buscando master (akamai)…" % ch)
    try:
        body, live_url = _fetch_master_live(info)
    except Exception as e:
        _dbg("[%s] Erro upstream: %s" % (ch, e))
        _trigger_refresh()
        return Response(
            "Stream temporariamente indisponivel. Renovando tokens, tente em 15s.",
            status=503, mimetype="text/plain")

    content = _rewrite_m3u8(body, live_url, ch)
    return Response(content, mimetype="application/x-mpegURL",
                    headers={"Cache-Control": "no-cache, no-store"})


@app.route("/channel/<ch>/<quality>")
def channel_quality(ch, quality):
    if ch not in CHANNELS:
        abort(404)
    if quality not in ("fhd", "hd", "sd"):
        abort(404)
    with lock:
        info = streams.get(ch)
    if not info:
        return Response(
            "Stream nao disponivel. Aguarde ~60s.\nDiagnostico: http://%s/debug" % req.host,
            status=503, mimetype="text/plain")

    if _is_simba(info):
        # ── Relay TS com qualidade específica ──
        variants = info.get("variants", {})
        if quality not in variants:
            return Response(
                "Qualidade '%s' nao disponivel para %s." % (quality, ch),
                status=404, mimetype="text/plain")
        return Response(
            _simba_relay_generator(ch, quality=quality),
            mimetype="video/MP2T",
            headers={
                "Cache-Control":    "no-cache, no-store",
                "X-Accel-Buffering":"no",
                "Connection":       "keep-alive",
            },
        )

    # ── M3U8 proxy Akamai com qualidade específica ──
    variants    = info.get("variants", {})
    variant_url = variants.get(quality)
    if not variant_url:
        return Response(
            "Qualidade '%s' nao disponivel para %s." % (quality, ch),
            status=404, mimetype="text/plain")

    try:
        master_body_live, live_url = _fetch_master_live(info)
        fresh_variants = _parse_variants(master_body_live, live_url)
        variant_url    = fresh_variants.get(quality) or variant_url
    except Exception as e:
        _dbg("[%s/%s] Erro ao re-buscar master: %s" % (ch, quality, e))

    try:
        r = _make_akamai_request(variant_url, info.get("hdntl", ""))
        r.raise_for_status()
    except Exception as e:
        _dbg("[%s/%s] Erro upstream: %s" % (ch, quality, e))
        _trigger_refresh()
        return Response(
            "Stream temporariamente indisponivel. Renovando tokens, tente em 15s.",
            status=503, mimetype="text/plain")

    content = _rewrite_m3u8(r.text, variant_url, ch)
    return Response(content, mimetype="application/x-mpegURL",
                    headers={"Cache-Control": "no-cache, no-store"})


# ── PROXY GENÉRICO (Akamai — usado pelo rewrite_m3u8) ─────────────────────────
@app.route("/proxy")
def proxy():
    url       = unquote(req.args.get("u", ""))
    ch        = req.args.get("_ch", "")
    if not url:
        abort(400)

    # Rotas cdnsimba nunca chegam aqui (relay faz tudo internamente),
    # mas por segurança bloqueamos caso cheguem.
    if "cdnsimba" in url:
        return Response("Use o endpoint /channel/<canal> para canais cdnsimba.",
                        status=400, mimetype="text/plain")

    hdntl_val = _hdntl_for_url(url)
    if not hdntl_val:
        with lock:
            vals = list(akamai_cookies.values())
        hdntl_val = vals[0] if vals else None

    try:
        up = _make_akamai_request(url, hdntl_val, stream=True)
        up.raise_for_status()
    except Exception as e:
        _dbg("proxy err: %s → %s" % (e, url[:80]), "warning")
        _trigger_refresh()
        return Response(
            "Token expirado, renovando. Tente em alguns segundos.",
            status=503, mimetype="text/plain",
            headers={"Retry-After": "5"},
        )

    ct = up.headers.get("Content-Type", "application/octet-stream")
    if "mpegURL" in ct or url.split("?")[0].endswith(".m3u8"):
        content = _rewrite_m3u8(up.text, url, ch)
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
    print("=" * 60, flush=True)
    print("  RecordPlus Proxy v4.0  |  porta %d" % PORT, flush=True)
    print("  Canais Akamai  → proxy M3U8 padrão", flush=True)
    print("  Canais cdnsimba → relay TS em tempo real", flush=True)
    print("=" * 60, flush=True)

    t_refresh = threading.Thread(target=_refresh_loop, daemon=True, name="refresh")
    t_origin  = threading.Thread(target=_origin_loop,  daemon=True, name="origin-renew")
    t_refresh.start()
    t_origin.start()

    print("Flask iniciado. Streams estarão disponíveis em ~2min.", flush=True)
    sys.stdout.flush()

    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
