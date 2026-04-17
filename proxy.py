#!/usr/bin/env python3
"""
RecordPlus HLS Proxy  v4
=========================
Links fixos para VLC:
  http://<IP>:8888/channel/sp
  http://<IP>:8888/channel/rio
  http://<IP>:8888/channel/minas

Diagnostico: http://<IP>:8888/debug

Arquitetura cdnsimba v4:
  - Thread dedicada por canal cdnsimba (sem Playwright no loop normal)
  - Renova bpk-token a cada TOKEN_REFRESH_INTERVAL segundos via requests puro
  - Token sempre pre-aquecido em memoria — zero latencia na requisicao
  - Playwright so e chamado quando origin_url expira (detectado automaticamente)
  - Playwright por canal individual (nao varre todos os canais)
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

# cdnsimba: renova bpk-token a cada N segundos sem Playwright
# O token dura ~60s; renovamos a cada 12s para ter margem confortavel
TOKEN_REFRESH_INTERVAL = 12   # segundos
TOKEN_TTL              = 55   # segundos — considerar token valido por ate 55s

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
#   "origin_url" : str,      # URL com JWT -> redireciona para cdn com bpk-token
#   "cache_base" : str|None, # https://cacheXX.cdnsimba.com.br:443
#   "token"      : str|None, # bpk-token atual (ex: 2ac@xxxxx)
#   "token_ts"   : float,    # timestamp da ultima renovacao
#   "stop"       : bool,     # sinaliza thread para parar
# }
_simba       = {}
_simba_lock  = threading.Lock()

_simba_recapturing      = set()
_simba_recapturing_lock = threading.Lock()


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


# ── CDNSIMBA: TOKEN POR CANAL (SEM PLAYWRIGHT) ────────────────────────────────

def _simba_fetch_token_once(ch, origin_url):
    """
    GET na origin_url sem seguir redirect -> extrai bpk-token do Location.
    Retorna True se obteve token novo, False se falhou.
    """
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
            _dbg("[simba] [%s] token renovado: %s…" % (ch, fresh_token[:18]))
            return True
        else:
            _dbg("[simba] [%s] Location invalida status=%d loc='%s'"
                 % (ch, r.status_code, location[:60]), "warning")
            return False
    except Exception as e:
        _dbg("[simba] [%s] erro HTTP: %s" % (ch, e), "warning")
        return False


def _simba_channel_thread(ch):
    """
    Thread dedicada por canal cdnsimba.
    Renova bpk-token a cada TOKEN_REFRESH_INTERVAL via requests puro.
    2 falhas consecutivas => origin_url expirou => dispara Playwright so deste canal.
    """
    _dbg("[simba] [%s] thread iniciada (refresh a cada %ds)" % (ch.upper(), TOKEN_REFRESH_INTERVAL))
    consecutive_failures = 0

    while True:
        time.sleep(TOKEN_REFRESH_INTERVAL)

        with _simba_lock:
            state = _simba.get(ch)
        if state is None or state.get("stop"):
            _dbg("[simba] [%s] thread encerrando" % ch.upper())
            break

        origin_url = state.get("origin_url", "")
        if not origin_url:
            continue

        ok = _simba_fetch_token_once(ch, origin_url)
        if ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            _dbg("[simba] [%s] falha consecutiva #%d" % (ch, consecutive_failures), "warning")
            if consecutive_failures >= 2:
                consecutive_failures = 0
                _trigger_simba_playwright_ch(ch)
                # Da tempo para a recaptura completar antes de tentar de novo
                time.sleep(TOKEN_REFRESH_INTERVAL * 3)


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


def _simba_register_channel(ch, origin_url, restart_thread=False):
    """Registra ou atualiza estado de canal cdnsimba e (re)inicia thread."""
    with _simba_lock:
        existing = _simba.get(ch, {})
        if restart_thread and existing:
            existing["stop"] = True   # sinaliza thread antiga para encerrar
        _simba[ch] = {
            "origin_url": origin_url,
            "cache_base": existing.get("cache_base"),
            "token":      existing.get("token"),
            "token_ts":   existing.get("token_ts", 0),
            "stop":       False,
        }

    t = threading.Thread(
        target=_simba_channel_thread,
        args=(ch,),
        daemon=True,
        name="simba-%s" % ch,
    )
    t.start()


def _trigger_simba_playwright_ch(ch):
    """
    Abre Playwright so para o canal ch (usa cookies de sessao, sem login).
    Captura nova origin_url e reinicia thread de renovacao deste canal.
    """
    global _session_cookies

    with _simba_recapturing_lock:
        if ch in _simba_recapturing:
            return
        _simba_recapturing.add(ch)

    def _run():
        try:
            if not _session_cookies:
                _dbg("[simba] [%s] sem cookies — aguardando captura inicial" % ch.upper())
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
                    if "cdnsimba" in url and \
                       ("index.m3u8" in url or "master.m3u8" in url) and \
                       not captured.get("origin_url"):
                        captured["origin_url"] = url

                page.on("request", _on_req)
                try:
                    page.goto(ch_url, wait_until="domcontentloaded", timeout=20000)
                except PWTimeout:
                    pass

                for _ in range(20):    # ate 10s
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
                # Reinicia thread com nova origin_url
                _simba_register_channel(ch, new_origin, restart_thread=True)
                # Busca token imediatamente
                time.sleep(0.3)
                _simba_fetch_token_once(ch, new_origin)
            else:
                _dbg("[simba] [%s] JWT NAO capturado — sessao expirou?" % ch.upper(), "warning")
                _trigger_refresh()

        except Exception:
            _dbg("[simba] [%s] ERRO Playwright:\n" % ch.upper() + traceback.format_exc())
        finally:
            with _simba_recapturing_lock:
                _simba_recapturing.discard(ch)

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


# ── RENOVACAO COMPLETA ────────────────────────────────────────────────────────

def _trigger_refresh():
    global _renewing
    with lock:
        if _renewing:
            return
        _renewing = True

    def _run():
        global _renewing
        _dbg("Renovacao de emergencia iniciada!")
        try:
            fetch_streams()
        except Exception:
            _dbg("ERRO na renovacao:\n" + traceback.format_exc())
        finally:
            global _renewing
            _renewing = False
            _dbg("Renovacao de emergencia concluida.")

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
                if "cdnsimba" in url and \
                   ("index.m3u8" in url or "master.m3u8" in url) and \
                   not _c.get("simba_origin_url"):
                    _c["simba_origin_url"] = url
                    _dbg("[playwright] [%s] origin_url capturada" % _ch.upper())

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
                _dbg("[playwright] [%s] FALHOU" % ch.upper())
                continue

            master_url = captured["master_url"]
            ak_domain  = urlparse(master_url).netloc

            if captured.get("needs_fetch") and not captured.get("master_body"):
                origin_url = master_url
                try:
                    r = requests.get(origin_url, headers={"User-Agent": UA},
                                     timeout=10, allow_redirects=True)
                    r.raise_for_status()
                    captured["master_body"] = r.text
                    captured["origin_url"]  = origin_url
                    if r.url != origin_url:
                        master_url = r.url
                        captured["master_url"] = master_url
                        ak_domain = urlparse(master_url).netloc
                    _dbg("[playwright] [%s] body OK (%d bytes)" % (ch.upper(), len(r.text)))
                except Exception as ex:
                    _dbg("[playwright] [%s] fallback falhou: %s" % (ch.upper(), ex))

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

            # Inicia/reinicia threads por canal cdnsimba
            for ch, info in new.items():
                origin_url = info.get("origin_url", "")
                if origin_url and "cdnsimba" in origin_url:
                    _simba_register_channel(ch, origin_url, restart_thread=True)
                    # Busca token imediatamente em background
                    threading.Thread(
                        target=_simba_fetch_token_once,
                        args=(ch, origin_url),
                        daemon=True,
                    ).start()
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
    """Re-busca master.m3u8 ao vivo com token fresco."""
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
            _dbg("[simba] [%s] token indisponivel para master" % ch, "warning")
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
<title>RecordPlus Proxy Login</title>
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
        variants = info.get("variants", {})
        is_simba = "cdnsimba" in info.get("origin_url", "")

        if is_simba:
            cb, tk = _simba_get_token(ch)
            with _simba_lock:
                st  = _simba.get(ch, {})
                age = int(time.time() - st.get("token_ts", 0))
            status = ("OK [token %ds atrás]" % age) if tk else ("SEM TOKEN" if ch in streams else "Aguardando")
        else:
            status = "OK" if ch in streams else "Aguardando"

        rows.append(
            "<tr>"
            "<td rowspan='4'><b>%s</b><br><small>%s</small></td>"
            "<td>Todos</td>"
            "<td><code>http://%s:%d/channel/%s</code></td>"
            "<td>%s</td>"
            "</tr>" % (ch.upper(), CHANNELS[ch]["name"], host, PORT, ch, status)
        )
        for q in ("fhd", "hd", "sd"):
            rows.append(
                "<tr><td>%s</td>"
                "<td><code>http://%s:%d/channel/%s/%s</code></td>"
                "<td>%s</td></tr>" % (
                    QUALITY_LABEL[q], host, PORT, ch, q,
                    "OK" if q in variants else "N/A")
            )

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>RecordPlus Proxy v4</title></head>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>RecordPlus HLS Proxy v4</h2>"
        "<p>VLC: <b>Media &rarr; Abrir fluxo de rede</b> &nbsp;|&nbsp; "
        "Token refresh cdnsimba: a cada <b>%ds</b></p>"
        "<table border='1' cellpadding='8' cellspacing='0'>"
        "<tr><th>Canal</th><th>Qualidade</th><th>URL para VLC</th><th>Status</th></tr>"
        % TOKEN_REFRESH_INTERVAL
        + "".join(rows) +
        "</table><br>"
        "<a href='/playlist.m3u' download='recordplus.m3u'>"
        "<button style='margin-right:12px;padding:8px 18px;font-size:14px;"
        "background:#e53935;color:#fff;border:none;border-radius:4px;cursor:pointer'>"
        "&#11123; Baixar playlist M3U</button></a> "
        "<a href='/debug'>Log de diagnóstico</a>"
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
        lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="RecordPlus",%s' % (
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
    return Response("\n".join(lines) + "\n", mimetype="audio/x-mpegurl",
                    headers={"Content-Disposition": "attachment; filename=recordplus.m3u"})


@app.route("/debug")
@_login_required
def debug():
    with lock:
        stream_info = {ch: {
            "url":        streams[ch].get("master_url", "")[:80],
            "domain":     streams[ch].get("ak_domain", ""),
            "hdntl":      "OK" if streams[ch].get("hdntl") else "MISSING",
            "origin_url": streams[ch].get("origin_url", "")[:60] or "N/A",
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

    lines = "\n".join(debug_log[-200:])
    return Response(
        "STREAMS:\n%s\n\nAKAMAI COOKIES:\n%s\n\n"
        "SIMBA TOKENS (refresh=%ds, ttl=%ds):\n%s\n\n"
        "Recapturando Playwright: %s\n\n%s\nLOG:\n%s" % (
            stream_info, list(akamai_cookies.keys()),
            TOKEN_REFRESH_INTERVAL, TOKEN_TTL, simba_info,
            list(_simba_recapturing),
            "=" * 50, lines),
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/channel/<ch>")
def channel(ch):
    if ch not in CHANNELS:
        abort(404)
    with lock:
        info = streams.get(ch)
    if not info:
        return Response("Stream nao disponivel. Aguarde ~60s.", status=503, mimetype="text/plain")

    try:
        body, live_url = _fetch_master_live(info, ch)
    except Exception as e:
        _dbg("[%s] Erro upstream: %s" % (ch, e), "warning")
        if info.get("origin_url") and "cdnsimba" in info.get("origin_url", ""):
            _trigger_simba_playwright_ch(ch)
        else:
            _trigger_refresh()
        return Response("Indisponivel. Tente em 10s.",
                        status=503, mimetype="text/plain", headers={"Retry-After": "10"})

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
        return Response("Stream nao disponivel.", status=503, mimetype="text/plain")

    variant_url = info.get("variants", {}).get(quality)
    if not variant_url:
        return Response("Qualidade '%s' nao disponivel para %s." % (quality, ch),
                        status=404, mimetype="text/plain")

    try:
        master_body_live, live_url = _fetch_master_live(info, ch)
        fresh_variants = _parse_variants(master_body_live, live_url)
        variant_url = fresh_variants.get(quality) or variant_url
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
        return Response("Indisponivel. Tente em 10s.",
                        status=503, mimetype="text/plain", headers={"Retry-After": "10"})

    content = _rewrite_m3u8(r.text, variant_url, ch)
    return Response(content, mimetype="application/x-mpegURL",
                    headers={"Cache-Control": "no-cache, no-store"})


@app.route("/proxy")
def proxy():
    url = unquote(req.args.get("u", ""))
    ch  = req.args.get("_ch", "")
    if not url:
        abort(400)

    is_simba = "cdnsimba" in url
    is_akamai = "akamai" in url

    # Para cdnsimba: substitui token na URL pelo token pre-aquecido
    if is_simba and ch:
        url = _simba_url_with_token(url, ch)
    elif is_simba:
        # Fallback sem canal: busca por dominio
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
        if not is_akamai and not is_simba:
            pass   # CDN aberta (DAI Google etc) — sem acao especial
        elif is_simba and ch:
            # Thread do canal ja esta gerenciando; nao precisamos fazer nada aqui
            # O proximo ciclo de TOKEN_REFRESH_INTERVAL ja vai renovar
            pass
        else:
            _trigger_refresh()
        return Response(
            "Token expirado. Tente em %ds." % TOKEN_REFRESH_INTERVAL,
            status=503, mimetype="text/plain",
            headers={"Retry-After": str(TOKEN_REFRESH_INTERVAL)},
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
    print("=" * 50, flush=True)
    print("  RecordPlus Proxy v4  |  porta %d" % PORT, flush=True)
    print("  TOKEN_REFRESH_INTERVAL = %ds" % TOKEN_REFRESH_INTERVAL, flush=True)
    print("=" * 50, flush=True)

    t = threading.Thread(target=_refresh_loop, daemon=True, name="refresh")
    t.start()

    print("Flask subindo. Streams em ~2min.", flush=True)
    sys.stdout.flush()

    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
