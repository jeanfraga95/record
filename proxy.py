#!/usr/bin/env python3
"""
RecordPlus HLS Proxy  v4.1
===========================
Fixes v4.1 vs v4:
  - Thread storm corrigido: usa threading.Event como stop signal (nao substituicao de dict)
  - origin_url: captura apenas master.m3u8 do dominio brasil.cdnsimba (nao variantes)
  - Playwright recaptura: cooldown de 30s por canal (evita recaptura em loop)
  - Recaptura Playwright roda em thread unica por canal (Event garante exclusao)
  - Thread registrada em _simba_threads[ch] — nova so sobe se anterior encerrou
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
EMAIL      = "jean.fraga20@gmail.com"
PASSWORD   = "qwerty123"
PROFILE_ID = "8a7ea0f8-c8c1-4a29-b424-14bbf7ee9275"

CHANNELS = {
    "sp":                  {"name": "Record SP",                    "event_id": "180", "group_id": "7"},
    "rio":                 {"name": "Record Rio",                   "event_id": "182", "group_id": "7"},
    "minas":               {"name": "Record Minas",                 "event_id": "186", "group_id": "7"},
    "guaiba":              {"name": "Record Guaiba",                "event_id": "178", "group_id": "7"},
    "bahia":               {"name": "Record Bahia",                 "event_id": "187", "group_id": "7"},
    "brasilia":            {"name": "Record Brasilia",              "event_id": "185", "group_id": "7"},
    "goias":               {"name": "Record Goias",                 "event_id": "189", "group_id": "7"},
    "belem":               {"name": "Record Belem",                 "event_id": "188", "group_id": "7"},
    "manaus":              {"name": "Record Manaus",                "event_id": "249", "group_id": "7"},
    "santos_vale":         {"name": "Record Santos e Vale",         "event_id": "597", "group_id": "7"},
    "bahia_itabuna":       {"name": "Record Bahia Itabuna",         "event_id": "598", "group_id": "7"},
    "bauru":               {"name": "Record Bauru",                 "event_id": "599", "group_id": "7"},
    "rio_preto":           {"name": "Record Rio Preto",             "event_id": "600", "group_id": "7"},
    "ribeirao_preto":      {"name": "Record Ribeirao Preto",        "event_id": "601", "group_id": "7"},
    "campos_goytacazes":   {"name": "Record Campos dos Goytacazes", "event_id": "602", "group_id": "7"},
    "Nsports":             {"name": "Nsports",                      "event_id": "644", "group_id": "76"},
    "Manual_do_Mundo":     {"name": "Manual do Mundo",              "event_id": "630", "group_id": "65"},
    "Acelerados":          {"name": "Acelerados",                   "event_id": "625", "group_id": "63"},
    "good_game_tv":        {"name": "Good Game TV",                 "event_id": "629", "group_id": "66"},
    "Desimpedidos":        {"name": "Desimpedidos",                 "event_id": "627", "group_id": "64"},
    "canal_do_artesanato": {"name": "Canal do Artesanato",          "event_id": "626", "group_id": "68"},
    "Record_news":         {"name": "Record News",                  "event_id": "191", "group_id": "10"},
}

PORT             = 8888
REFRESH_INTERVAL = 1500   # 25 min — renovacao completa via Playwright

# bpk-token: renova a cada N segundos via requests puro (sem Playwright)
TOKEN_REFRESH_INTERVAL = 15   # segundos
TOKEN_TTL              = 55   # considerar valido por ate 55s

# Playwright por canal: minimo 30s entre recapturas consecutivas
PW_COOLDOWN = 30   # segundos

BASE_URL = "https://www.recordplus.com"
PANEL_USER     = "admin"
PANEL_PASSWORD = "admin123"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
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
    if len(debug_log) > 500:
        debug_log.pop(0)

# ── ESTADO GLOBAL ─────────────────────────────────────────────────────────────
streams        = {}
akamai_cookies = {}
lock           = threading.Lock()
_renewing      = False
_session_cookies = []

# ── ESTADO POR CANAL CDNSIMBA ─────────────────────────────────────────────────
# _simba[ch] = {
#   "origin_url"  : str,            # URL JWT brasil.cdnsimba.com.br/...?auth=...
#   "cache_base"  : str|None,       # https://cacheXX.cdnsimba.com.br:443
#   "token"       : str|None,       # bpk-token atual
#   "token_ts"    : float,          # timestamp da ultima renovacao
#   "stop_event"  : threading.Event # set() para encerrar a thread
#   "pw_last_ts"  : float           # timestamp da ultima recaptura Playwright
# }
_simba      = {}
_simba_lock = threading.Lock()

# Referencia para threads ativas por canal (evita duplicatas)
_simba_threads     = {}   # ch -> threading.Thread
_simba_thread_lock = threading.Lock()

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
    headers = {"User-Agent": UA, "Referer": BASE_URL + "/", "Origin": BASE_URL}
    if hdntl_val:
        headers["Cookie"] = "hdntl=%s" % hdntl_val
    return requests.get(url, headers=headers, stream=stream, timeout=15)

def _hdntl_for_url(url):
    domain = urlparse(url).netloc
    if "akamai" not in domain:
        return None
    with lock:
        if domain in akamai_cookies:
            return akamai_cookies[domain]
        for d, v in akamai_cookies.items():
            if d in domain or domain in d:
                return v
    return None

# ── CDNSIMBA: RENOVACAO DE TOKEN POR CANAL ────────────────────────────────────

def _simba_fetch_token_once(ch):
    """
    Faz GET na origin_url sem seguir redirect.
    Extrai bpk-token do header Location.
    Retorna True se OK, False se falhou (JWT expirado ou erro de rede).
    """
    with _simba_lock:
        state = _simba.get(ch, {})
        origin_url = state.get("origin_url", "")

    if not origin_url:
        return False

    try:
        r = requests.get(
            origin_url,
            headers={"User-Agent": UA, "Referer": BASE_URL + "/", "Origin": BASE_URL},
            timeout=8,
            allow_redirects=False,
        )
        location = r.headers.get("Location", "")
        m = re.match(r"(https://[^/]+)/bpk-token/([^/]+)/", location)
        if m:
            cache_base  = m.group(1)
            fresh_token = m.group(2)
            with _simba_lock:
                if ch in _simba:
                    _simba[ch]["cache_base"] = cache_base
                    _simba[ch]["token"]      = fresh_token
                    _simba[ch]["token_ts"]   = time.time()
            _dbg("[simba] [%s] token OK: %s…" % (ch, fresh_token[:18]))
            return True
        else:
            _dbg("[simba] [%s] JWT invalido status=%d — origin_url expirou" % (ch, r.status_code), "warning")
            return False
    except Exception as e:
        _dbg("[simba] [%s] erro HTTP: %s" % (ch, e), "warning")
        return False


def _simba_channel_thread(ch, stop_event):
    """
    Thread dedicada por canal cdnsimba.
    Renova bpk-token a cada TOKEN_REFRESH_INTERVAL usando stop_event para aguardar.
    Se falhar 2x consecutivas, dispara Playwright apenas para este canal.
    """
    _dbg("[simba] [%s] thread iniciada" % ch.upper())
    failures = 0

    while not stop_event.is_set():
        # Aguarda TOKEN_REFRESH_INTERVAL ou encerra imediatamente se stop_event for setado
        if stop_event.wait(timeout=TOKEN_REFRESH_INTERVAL):
            break   # stop_event foi setado

        ok = _simba_fetch_token_once(ch)
        if ok:
            failures = 0
        else:
            failures += 1
            if failures >= 2:
                failures = 0
                _trigger_simba_playwright_ch(ch)
                # Aguarda mais antes de tentar de novo
                if stop_event.wait(timeout=TOKEN_REFRESH_INTERVAL * 2):
                    break

    _dbg("[simba] [%s] thread encerrada" % ch.upper())


def _simba_get_token(ch):
    """Retorna (cache_base, token) se valido, senao (None, None)."""
    with _simba_lock:
        state = _simba.get(ch, {})
    token      = state.get("token")
    cache_base = state.get("cache_base")
    token_ts   = state.get("token_ts", 0)
    if token and cache_base and (time.time() - token_ts) < TOKEN_TTL:
        return cache_base, token
    return None, None


def _simba_register_channel(ch, origin_url):
    """
    Registra ou atualiza estado de canal cdnsimba.
    Para thread existente via stop_event e inicia nova thread.
    """
    # Para thread anterior se existir
    with _simba_thread_lock:
        old_thread = _simba_threads.get(ch)

    if old_thread and old_thread.is_alive():
        with _simba_lock:
            state = _simba.get(ch, {})
            old_event = state.get("stop_event")
        if old_event:
            old_event.set()   # sinaliza encerramento imediato
        old_thread.join(timeout=TOKEN_REFRESH_INTERVAL + 2)

    stop_event = threading.Event()

    with _simba_lock:
        existing = _simba.get(ch, {})
        _simba[ch] = {
            "origin_url": origin_url,
            "cache_base": existing.get("cache_base"),
            "token":      existing.get("token"),
            "token_ts":   existing.get("token_ts", 0),
            "stop_event": stop_event,
            "pw_last_ts": existing.get("pw_last_ts", 0),
        }

    t = threading.Thread(
        target=_simba_channel_thread,
        args=(ch, stop_event),
        daemon=True,
        name="simba-%s" % ch,
    )
    with _simba_thread_lock:
        _simba_threads[ch] = t
    t.start()
    _dbg("[simba] [%s] thread registrada com nova origin_url" % ch.upper())


def _trigger_simba_playwright_ch(ch):
    """
    Abre Playwright APENAS para o canal ch.
    Tem cooldown de PW_COOLDOWN segundos para evitar recapturas em loop.
    """
    global _session_cookies

    now = time.time()
    with _simba_lock:
        state = _simba.get(ch, {})
        last_pw = state.get("pw_last_ts", 0)
        if now - last_pw < PW_COOLDOWN:
            _dbg("[simba] [%s] Playwright em cooldown (%.0fs restantes)" % (
                ch, PW_COOLDOWN - (now - last_pw)))
            return
        # Marca timestamp imediatamente para evitar disparo paralelo
        if ch in _simba:
            _simba[ch]["pw_last_ts"] = now

    def _run():
        try:
            if not _session_cookies:
                _dbg("[simba] [%s] sem cookies de sessao" % ch.upper(), "warning")
                return

            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
            _dbg("[simba] [%s] Playwright recapturando JWT…" % ch.upper())

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

                def _on_req(request):
                    url = request.url
                    # Captura APENAS o master/index principal do canal (brasil.cdnsimba)
                    # Ignora variantes HLS (bandwidth/xxx.m3u8) e segmentos .ts
                    if ("brasil.cdnsimba" in url or "cdnsimba" in url) and \
                       not captured.get("origin_url") and \
                       ("index.m3u8" in url or "master.m3u8" in url) and \
                       "bandwidth" not in url and \
                       "variant" not in url:
                        captured["origin_url"] = url
                        _dbg("[simba] [%s] origin_url capturada: …%s" % (
                            ch.upper(), url[-50:]))

                page.on("request", _on_req)
                try:
                    page.goto(ch_url, wait_until="domcontentloaded", timeout=20000)
                except PWTimeout:
                    pass

                for _ in range(24):   # ate 12s
                    if captured.get("origin_url"):
                        break
                    page.wait_for_timeout(500)

                page.remove_listener("request", _on_req)
                browser.close()

            new_origin = captured.get("origin_url")
            if new_origin:
                _dbg("[simba] [%s] JWT capturado via Playwright" % ch.upper())
                with lock:
                    if ch in streams:
                        streams[ch]["origin_url"] = new_origin
                # Atualiza origin_url no estado simba SEM reiniciar thread
                with _simba_lock:
                    if ch in _simba:
                        _simba[ch]["origin_url"] = new_origin
                # Busca token imediatamente com o novo JWT
                time.sleep(0.2)
                ok = _simba_fetch_token_once(ch)
                if ok:
                    _dbg("[simba] [%s] token obtido apos recaptura" % ch.upper())
                else:
                    _dbg("[simba] [%s] token FALHOU mesmo apos recaptura" % ch.upper(), "warning")
            else:
                _dbg("[simba] [%s] JWT nao capturado — sessao expirou?" % ch.upper(), "warning")
                _trigger_refresh()

        except Exception:
            _dbg("[simba] [%s] ERRO Playwright:\n" % ch.upper() + traceback.format_exc())

    threading.Thread(target=_run, daemon=True, name="simba-pw-%s" % ch).start()


def _simba_url_with_token(orig_url, ch):
    """Substitui bpk-token na URL pelo token atual do canal."""
    cache_base, token = _simba_get_token(ch)
    if not cache_base or not token:
        return orig_url
    m = re.search(r"/bpk-token/[^/]+(/.*)", orig_url)
    if not m:
        return orig_url
    return "%s/bpk-token/%s%s" % (cache_base, token, m.group(1))

# ── RENOVACAO COMPLETA VIA PLAYWRIGHT ────────────────────────────────────────

def _trigger_refresh():
    global _renewing
    with lock:
        if _renewing:
            return
        _renewing = True

    def _run():
        global _renewing
        _dbg("Renovacao completa iniciada!")
        try:
            fetch_streams()
        except Exception:
            _dbg("ERRO na renovacao:\n" + traceback.format_exc())
        finally:
            global _renewing
            _renewing = False
            _dbg("Renovacao completa concluida.")

    threading.Thread(target=_run, daemon=True, name="emergency-refresh").start()


def _fetch_via_playwright():
    _dbg("[playwright] Captura completa iniciada…")
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

        _dbg("[playwright] Fazendo login…")
        try:
            page.goto(BASE_URL + "/account/login", wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            pass

        page.wait_for_timeout(4000)
        page.fill("#UserName", EMAIL)
        page.wait_for_timeout(500)
        page.fill("#Password", PASSWORD)
        page.wait_for_timeout(500)

        try:
            page.click("button.main-button", timeout=10000)
        except Exception:
            try:
                page.locator("button[type=submit]").last.click(timeout=10000)
            except Exception:
                page.evaluate("document.querySelector('form').submit()")

        try:
            page.wait_for_url("**/escolhaseuperfil**", timeout=20000)
        except PWTimeout:
            _dbg("[playwright] URL apos submit: " + page.url)

        if "escolhaseuperfil" in page.url.lower():
            try:
                page.click("img.profile-img[onclick*='%s']" % PROFILE_ID, timeout=5000)
            except Exception:
                try:
                    page.click("img.profile-img", timeout=5000)
                except Exception:
                    pass
            try:
                page.wait_for_url("**/home**", timeout=15000)
            except PWTimeout:
                page.wait_for_timeout(3000)
            _dbg("[playwright] URL apos perfil: " + page.url)

        global _session_cookies
        _session_cookies = ctx.cookies()
        _dbg("[playwright] %d cookies de sessao salvos" % len(_session_cookies))

        for ch, info in CHANNELS.items():
            ch_url   = "%s/Live/LiveEvent/%s?groupId=%s" % (
                BASE_URL, info["event_id"], info["group_id"])
            captured = {}

            def _on_req(request, _c=captured, _ch=ch):
                url = request.url
                # Captura origin_url cdnsimba: apenas master/index, nao variantes
                if ("brasil.cdnsimba" in url or "cdnsimba" in url) and \
                   not _c.get("simba_origin_url") and \
                   ("index.m3u8" in url or "master.m3u8" in url) and \
                   "bandwidth" not in url and \
                   "variant" not in url:
                    _c["simba_origin_url"] = url
                    _dbg("[playwright] [%s] origin_url: …%s" % (_ch.upper(), url[-50:]))

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
                        _dbg("[playwright] [%s] master %d bytes" % (_ch.upper(), len(body)))
                    except Exception:
                        _c["master_url"]  = resp.url
                        _c["needs_fetch"] = True
                        _dbg("[playwright] [%s] redirect, fetch posterior" % _ch.upper())

            page.on("request", _on_req)
            page.on("response", _on_resp)
            _dbg("[playwright] [%s] Navegando…" % ch.upper())
            try:
                page.goto(ch_url, wait_until="networkidle", timeout=30000)
            except PWTimeout:
                page.wait_for_timeout(3000)

            for _ in range(30):
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
                _dbg("[playwright] [%s] FALHOU — sem master.m3u8" % ch.upper(), "warning")
                continue

            master_url = captured["master_url"]
            ak_domain  = urlparse(master_url).netloc

            if captured.get("needs_fetch") and not captured.get("master_body"):
                origin_try = master_url
                try:
                    r = requests.get(origin_try, headers={"User-Agent": UA},
                                     timeout=10, allow_redirects=True)
                    r.raise_for_status()
                    captured["master_body"] = r.text
                    captured["origin_url"]  = origin_try
                    if r.url != origin_try:
                        master_url = r.url
                        captured["master_url"] = master_url
                        ak_domain = urlparse(master_url).netloc
                    _dbg("[playwright] [%s] body OK %d bytes" % (ch.upper(), len(r.text)))
                except Exception as ex:
                    _dbg("[playwright] [%s] fallback falhou: %s" % (ch.upper(), ex), "warning")

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
            _dbg("[playwright] [%s] variantes: %s" % (ch.upper(), list(variants.keys())))

            # origin_url final: prefere simba_origin_url (interceptada no request),
            # fallback para origin_url (URL pre-redirect capturada no needs_fetch)
            origin_url_final = captured.get("simba_origin_url") or captured.get("origin_url", "")
            results[ch] = {
                "master_url":  master_url,
                "master_body": master_body,
                "hdntl":       hdntl_val or "",
                "ak_domain":   ak_domain,
                "variants":    variants,
                "origin_url":  origin_url_final,
            }

        browser.close()

    return results, new_ak_cookies


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

            # Para cada canal cdnsimba: registra e busca token imediatamente
            for ch, info in new.items():
                origin_url = info.get("origin_url", "")
                if origin_url and "cdnsimba" in origin_url:
                    _simba_register_channel(ch, origin_url)
                    # Busca token imediatamente (nao espera o primeiro ciclo de 15s)
                    ok = _simba_fetch_token_once(ch)
                    if ok:
                        _dbg("[simba] [%s] token inicial OK" % ch)
                    else:
                        _dbg("[simba] [%s] token inicial FALHOU — JWT ja expirou no Playwright?" % ch, "warning")
                        # Dispara recaptura imediata se JWT inicial falhou
                        _trigger_simba_playwright_ch(ch)
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
        _dbg("Proxima renovacao completa em %d min" % (REFRESH_INTERVAL // 60))
        time.sleep(REFRESH_INTERVAL)


# ── REESCRITA M3U8 ────────────────────────────────────────────────────────────
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
            return 'URI="%s"' % _proxy_url(_abs_url(m.group(1), base_root), ch)
        return re.sub(r'URI="([^"]+)"', _replace, line)

    for line in content.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            out.append(_rewrite_uri_attr(line) if 'URI="' in s else line)
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


def _fetch_master_live(info, ch=""):
    origin_url = info.get("origin_url", "")
    master_url = info["master_url"]
    hdntl_val  = info.get("hdntl", "")

    if origin_url and "cdnsimba" in origin_url:
        cache_base, token = _simba_get_token(ch) if ch else (None, None)
        if cache_base and token:
            m = re.search(r"/bpk-token/[^/]+(/.*)", master_url)
            fresh_url = ("%s/bpk-token/%s%s" % (cache_base, token, m.group(1))
                         if m else master_url)
        else:
            _dbg("[simba] [%s] sem token valido — usando URL cacheada" % ch, "warning")
            fresh_url = master_url
        r = requests.get(
            fresh_url,
            headers={"User-Agent": UA, "Referer": BASE_URL + "/", "Origin": BASE_URL},
            timeout=10,
        )
        r.raise_for_status()
        return r.text, fresh_url
    else:
        r = _make_akamai_request(master_url, hdntl_val)
        r.raise_for_status()
        return r.text, master_url


# ── FLASK ─────────────────────────────────────────────────────────────────────
import secrets
from functools import wraps
from flask import session, redirect

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

def _check_auth(u, p):
    return (secrets.compare_digest(u, PANEL_USER) and
            secrets.compare_digest(p, PANEL_PASSWORD))

def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>RecordPlus Proxy Login</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#1a1a2e;display:flex;justify-content:center;
       align-items:center;min-height:100vh;font-family:sans-serif}}
  .card{{background:#16213e;border-radius:12px;padding:40px 36px;
         width:340px;box-shadow:0 8px 32px rgba(0,0,0,.4)}}
  h2{{color:#e0e0e0;text-align:center;margin-bottom:8px;font-size:20px}}
  p.sub{{color:#888;text-align:center;font-size:13px;margin-bottom:28px}}
  label{{color:#aaa;font-size:13px;display:block;margin-bottom:6px}}
  input{{width:100%;padding:10px 14px;border-radius:6px;border:1px solid #2a4080;
         background:#0f3460;color:#e0e0e0;font-size:14px;margin-bottom:18px;outline:none}}
  input:focus{{border-color:#e53935}}
  button{{width:100%;padding:11px;background:#e53935;color:#fff;border:none;
          border-radius:6px;font-size:15px;font-weight:bold;cursor:pointer}}
  button:hover{{background:#c62828}}
  .error{{background:#4a0000;color:#ff8a80;border-radius:6px;
          padding:10px 14px;font-size:13px;margin-bottom:16px;text-align:center}}
</style></head>
<body><div class='card'>
<h2>&#128250; RecordPlus Proxy</h2><p class='sub'>Acesso restrito</p>
{error}
<form method='POST' action='/login'>
<label>Usuário</label><input type='text' name='username' autofocus>
<label>Senha</label><input type='password' name='password'>
<button type='submit'>Entrar</button>
</form></div></body></html>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if req.method == "POST":
        if _check_auth(req.form.get("username",""), req.form.get("password","")):
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
    QL = {"fhd": "Full HD 1080p", "hd": "HD 720p", "sd": "SD 480p"}
    rows = []
    for ch in CHANNELS:
        with lock:
            info = streams.get(ch, {})
        variants = info.get("variants", {})
        is_simba = "cdnsimba" in info.get("origin_url", "")

        if is_simba:
            cb, tk = _simba_get_token(ch)
            with _simba_lock:
                st  = _simba.get(ch, {})
                age = int(time.time() - st.get("token_ts", 0))
            if tk:
                status = "OK [token %ds atras]" % age
            elif ch in streams:
                status = "SEM TOKEN"
            else:
                status = "Aguardando"
        else:
            status = "OK" if ch in streams else "Aguardando"

        rows.append(
            "<tr><td rowspan='4'><b>%s</b><br><small>%s</small></td>"
            "<td>Todos</td><td><code>http://%s:%d/channel/%s</code></td>"
            "<td>%s</td></tr>" % (ch.upper(), CHANNELS[ch]["name"], host, PORT, ch, status)
        )
        for q in ("fhd", "hd", "sd"):
            rows.append(
                "<tr><td>%s</td><td><code>http://%s:%d/channel/%s/%s</code></td>"
                "<td>%s</td></tr>" % (QL[q], host, PORT, ch, q, "OK" if q in variants else "N/A")
            )

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>RecordPlus Proxy v4.1</title></head>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>RecordPlus HLS Proxy v4.1</h2>"
        "<p>VLC: <b>Media &rarr; Abrir fluxo de rede</b> | "
        "Token refresh: <b>%ds</b> | Cooldown PW: <b>%ds</b></p>"
        "<table border='1' cellpadding='8' cellspacing='0'>"
        "<tr><th>Canal</th><th>Qualidade</th><th>URL VLC</th><th>Status</th></tr>"
        % (TOKEN_REFRESH_INTERVAL, PW_COOLDOWN)
        + "".join(rows) +
        "</table><br>"
        "<a href='/playlist.m3u'><button style='margin-right:12px;padding:8px 18px;"
        "background:#e53935;color:#fff;border:none;border-radius:4px;cursor:pointer'>"
        "Baixar M3U</button></a>"
        "<a href='/debug'>Diagnóstico</a></body></html>"
    )

@app.route("/playlist.m3u")
@_login_required
def playlist_m3u():
    host = req.host.split(":")[0]
    QL = {"fhd": "Full HD 1080p", "hd": "HD 720p", "sd": "SD 480p"}
    lines = ["#EXTM3U"]
    with lock:
        snap = dict(streams)
    for ch, info in snap.items():
        n = CHANNELS[ch]["name"]
        v = info.get("variants", {})
        lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="RecordPlus",%s' % (ch, n, n))
        lines.append("http://%s:%d/channel/%s" % (host, PORT, ch))
        for q in ("fhd", "hd", "sd"):
            if q not in v:
                continue
            label = "%s (%s)" % (n, QL[q])
            lines.append('#EXTINF:-1 tvg-id="%s_%s" tvg-name="%s" group-title="RecordPlus %s",%s' % (
                ch, q, label, QL[q], label))
            lines.append("http://%s:%d/channel/%s/%s" % (host, PORT, ch, q))
    return Response("\n".join(lines) + "\n", mimetype="audio/x-mpegurl",
                    headers={"Content-Disposition": "attachment; filename=recordplus.m3u"})

@app.route("/debug")
@_login_required
def debug():
    with lock:
        si = {ch: {
            "url":        streams[ch].get("master_url", "")[:80],
            "hdntl":      "OK" if streams[ch].get("hdntl") else "MISSING",
            "origin_url": streams[ch].get("origin_url", "")[:70] or "N/A",
        } for ch in streams}

    with _simba_lock:
        simba_info = {}
        for ch, st in _simba.items():
            age = int(time.time() - st.get("token_ts", 0))
            simba_info[ch] = {
                "token": (st.get("token") or "NONE")[:22],
                "age_s": age,
                "valid": age < TOKEN_TTL and bool(st.get("token")),
            }

    with _simba_thread_lock:
        thread_status = {ch: t.is_alive() for ch, t in _simba_threads.items()}

    lines = "\n".join(debug_log[-200:])
    return Response(
        "STREAMS (%d):\n%s\n\nAKAMAI: %s\n\n"
        "SIMBA TOKENS (refresh=%ds ttl=%ds pw_cooldown=%ds):\n%s\n\n"
        "THREADS ATIVAS: %s\n\n%s\nLOG:\n%s" % (
            len(streams), si, list(akamai_cookies.keys()),
            TOKEN_REFRESH_INTERVAL, TOKEN_TTL, PW_COOLDOWN, simba_info,
            thread_status, "=" * 50, lines),
        mimetype="text/plain; charset=utf-8",
    )

@app.route("/channel/<ch>")
def channel(ch):
    if ch not in CHANNELS:
        abort(404)
    with lock:
        info = streams.get(ch)
    if not info:
        return Response("Stream indisponivel. Aguarde ~60s.", status=503, mimetype="text/plain")

    try:
        body, live_url = _fetch_master_live(info, ch)
    except Exception as e:
        _dbg("[%s] Erro upstream: %s" % (ch, e), "warning")
        if info.get("origin_url") and "cdnsimba" in info.get("origin_url", ""):
            _trigger_simba_playwright_ch(ch)
        else:
            _trigger_refresh()
        return Response("Indisponivel. Tente em 10s.", status=503, mimetype="text/plain",
                        headers={"Retry-After": "10"})

    return Response(_rewrite_m3u8(body, live_url, ch), mimetype="application/x-mpegURL",
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
        return Response("Stream indisponivel.", status=503, mimetype="text/plain")

    variant_url = info.get("variants", {}).get(quality)
    if not variant_url:
        return Response("Qualidade '%s' nao disponivel para %s." % (quality, ch),
                        status=404, mimetype="text/plain")

    try:
        mbody, live_url = _fetch_master_live(info, ch)
        fv = _parse_variants(mbody, live_url)
        variant_url = fv.get(quality) or variant_url
    except Exception as e:
        _dbg("[%s/%s] Erro re-buscar master: %s" % (ch, quality, e), "warning")

    hdntl_val = info.get("hdntl", "")
    try:
        if "akamai" in variant_url:
            r = _make_akamai_request(variant_url, hdntl_val)
        else:
            r = requests.get(variant_url, headers={"User-Agent": UA,
                             "Referer": BASE_URL + "/", "Origin": BASE_URL}, timeout=12)
        r.raise_for_status()
    except Exception as e:
        _dbg("[%s/%s] Erro upstream: %s" % (ch, quality, e), "warning")
        if info.get("origin_url") and "cdnsimba" in info.get("origin_url", ""):
            _trigger_simba_playwright_ch(ch)
        else:
            _trigger_refresh()
        return Response("Indisponivel. Tente em 10s.", status=503, mimetype="text/plain",
                        headers={"Retry-After": "10"})

    return Response(_rewrite_m3u8(r.text, variant_url, ch), mimetype="application/x-mpegURL",
                    headers={"Cache-Control": "no-cache, no-store"})

@app.route("/proxy")
def proxy():
    url = unquote(req.args.get("u", ""))
    ch  = req.args.get("_ch", "")
    if not url:
        abort(400)

    is_simba = "cdnsimba" in url
    is_akamai = "akamai" in url

    if is_simba and ch:
        url = _simba_url_with_token(url, ch)
    elif is_simba:
        req_domain = urlparse(url).netloc
        with _simba_lock:
            for c, st in _simba.items():
                if req_domain in st.get("cache_base", ""):
                    url = _simba_url_with_token(url, c)
                    ch  = c
                    break

    hdntl_val = _hdntl_for_url(url) if is_akamai else None

    try:
        if hdntl_val:
            up = _make_akamai_request(url, hdntl_val, stream=True)
        else:
            up = requests.get(
                url,
                headers={"User-Agent": UA, "Referer": BASE_URL + "/", "Origin": BASE_URL},
                stream=True, timeout=15, allow_redirects=True,
            )
        up.raise_for_status()
    except Exception as e:
        _dbg("proxy err [%s]: %s -> %s" % (ch or "?", e, url[:80]), "warning")
        if is_simba and ch:
            _trigger_simba_playwright_ch(ch)
        elif not is_akamai and not is_simba:
            pass
        else:
            _trigger_refresh()
        return Response("Token expirado. Tente em %ds." % TOKEN_REFRESH_INTERVAL,
                        status=503, mimetype="text/plain",
                        headers={"Retry-After": str(TOKEN_REFRESH_INTERVAL)})

    ct = up.headers.get("Content-Type", "application/octet-stream")
    if "mpegURL" in ct or url.split("?")[0].endswith(".m3u8"):
        return Response(_rewrite_m3u8(up.text, url, ch), mimetype="application/x-mpegURL",
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
    print("  RecordPlus Proxy v4.1  |  porta %d" % PORT, flush=True)
    print("  TOKEN_REFRESH=%ds  PW_COOLDOWN=%ds" % (TOKEN_REFRESH_INTERVAL, PW_COOLDOWN), flush=True)
    print("=" * 50, flush=True)

    threading.Thread(target=_refresh_loop, daemon=True, name="refresh").start()

    print("Flask subindo. Streams em ~2min.", flush=True)
    sys.stdout.flush()
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
