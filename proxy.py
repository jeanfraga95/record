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
CHANNELS = {     "sp": {"name": "Record SP", "event_id": "180", "group_id": "7"},     
            "rio": {"name": "Record Rio", "event_id": "182", "group_id": "7"},
            "minas": {"name": "Record Minas", "event_id": "186", "group_id": "7"},
            "guaiba": {"name": "Record Guaiba", "event_id": "178", "group_id": "7"},
            "bahia": {"name": "Record Bahia", "event_id": "187", "group_id": "7"},
            "brasilia": {"name": "Record Brasilia", "event_id": "185", "group_id": "7"},
            "goias": {"name": "Record Goias", "event_id": "189", "group_id": "7"},
            "belem": {"name": "Record Belem", "event_id": "188", "group_id": "7"},
            "manaus": {"name": "Record Manaus", "event_id": "249", "group_id": "7"},
            "santos_vale": {"name": "Record Santos e Vale", "event_id": "597", "group_id": "7"},
            "bahia_itabuna": {"name": "Record Bahia Itabuna", "event_id": "598", "group_id": "7"},
            "bauru": {"name": "Record Bauru", "event_id": "599", "group_id": "7"},
            "rio_preto": {"name": "Record Rio Preto", "event_id": "600", "group_id": "7"},
            "ribeirao_preto": {"name": "Record Ribeirao Preto", "event_id": "601", "group_id": "7"},
            "campos_goytacazes": {"name": "Record Campos dos Goytacazes", "event_id": "602", "group_id": "7"},
            "Nsports": {"name":"Nsports", "event_id": "644","group_id":"76"},
            "Manual_do_Mundo": {"name":"Manual do Mundo", "event_id": "630","group_id":"65"},
            "Acelerados": {"name":"Acelerados", "event_id": "625","group_id":"63"},
             "good_game_tv": {"name":"Good Game TV", "event_id": "629","group_id":"66"},
             "Desimpedidos": {"name":"Nsports", "event_id": "627","group_id":"64"},
             "canal_do_artesanato": {"name":"Canal do Artesanato", "event_id": "626","group_id":"68"},
             "Record_news": {"name":"Record News", "event_id": "191","group_id":"10"},
             "Desimpedidos": {"name":"Nsports", "event_id": "627","group_id":"64"},
            
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
_renewing      = False   # evita renovacoes simultaneas
# Cache de token cdnsimba: { origin_url -> {"token": str, "cache_base": str, "ts": float} }
_simba_token_cache = {}
_simba_lock        = threading.Lock()
_session_cookies   = []   # cookies Playwright salvos apos login

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
    """Retorna o cookie hdntl correto para uma URL Akamai (None se nao necessario)."""
    domain = urlparse(url).netloc
    # CDNs abertos nao precisam de hdntl
    if "akamai" not in domain:
        return None
    with lock:
        # Busca exata
        if domain in akamai_cookies:
            return akamai_cookies[domain]
        # Busca parcial
        for d, v in akamai_cookies.items():
            if d in domain or domain in d:
                return v
    return None


def _trigger_refresh():
    """Dispara renovacao imediata em background se nao houver uma em andamento."""
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

    t = threading.Thread(target=_run, daemon=True, name="emergency-refresh")
    t.start()


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

        # Salva cookies da sessão para re-uso posterior sem novo login
        global _session_cookies
        _session_cookies = ctx.cookies()
        _dbg("[playwright] %d cookies salvos para reuso" % len(_session_cookies))

        # Captura por canal
        for ch, info in CHANNELS.items():
            ch_url   = "%s/Live/LiveEvent/%s?groupId=%s" % (
                BASE_URL, info["event_id"], info["group_id"])
            captured = {}

            # Intercepta REQUEST para cdnsimba (captura origin URL com auth JWT)
            def _on_req(request, _c=captured, _ch=ch):
                url = request.url
                if ("cdnsimba" in url or "brasil.cdnsimba" in url) and                    ("index.m3u8" in url or "master.m3u8" in url) and                    not _c.get("simba_origin_url"):
                    _c["simba_origin_url"] = url
                    _dbg("[playwright] [%s] origin_url capturada" % _ch.upper())

            page.on("request", _on_req)

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
                        # Redirect response: body indisponivel, guarda URL para fetch posterior
                        _c["master_url"] = resp.url
                        _c["needs_fetch"] = True
                        _dbg("[playwright] [%s] redirect detectado, fetch posterior" % _ch.upper())

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

            # Canais com redirect (cdnsimba etc): busca body via requests direto
            if captured.get("needs_fetch") and not captured.get("master_body"):
                origin_url = master_url   # URL original antes do redirect (token curto)
                _dbg("[playwright] [%s] Buscando body via requests (%s)…" % (
                    ch.upper(), ak_domain))
                try:
                    r = requests.get(origin_url, headers={"User-Agent": UA}, timeout=10,
                                     allow_redirects=True)
                    r.raise_for_status()
                    captured["master_body"] = r.text
                    captured["origin_url"]  = origin_url   # guarda URL pre-redirect
                    # Atualiza URL final apos redirect (contém token curto, só p/ debug)
                    if r.url != origin_url:
                        master_url = r.url
                        captured["master_url"] = master_url
                        ak_domain = urlparse(master_url).netloc
                    _dbg("[playwright] [%s] body via requests OK (%d bytes)" % (
                        ch.upper(), len(r.text)))
                except Exception as ex:
                    _dbg("[playwright] [%s] requests fallback falhou: %s" % (
                        ch.upper(), ex))

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

            master_body = captured.get("master_body", "")
            variants    = _parse_variants(master_body, master_url) if master_body else {}
            _dbg("[playwright] [%s] variantes: %s" % (ch.upper(), list(variants.keys())))

            results[ch] = {
                "master_url":  master_url,
                "master_body": master_body,
                "hdntl":       hdntl_val or "",
                "ak_domain":   ak_domain,
                "variants":    variants,
                "origin_url":  captured.get("simba_origin_url") or captured.get("origin_url", ""),
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


def _warmup_simba_tokens():
    """
    Aquece o cache de tokens cdnsimba para todos os canais capturados.
    Chamado logo após fetch_streams() e a cada 3 minutos em background.
    """
    with lock:
        simba_channels = {
            ch: info for ch, info in streams.items()
            if info.get("origin_url") and "cdnsimba" in info.get("origin_url", "")
        }
    if not simba_channels:
        return
    _dbg("[simba] Aquecendo tokens para: %s" % list(simba_channels.keys()))
    for ch, info in simba_channels.items():
        origin_url = info["origin_url"]
        try:
            cache_base, token = _get_fresh_simba_token(origin_url)
            if token:
                _dbg("[simba] [%s] token OK (%s...)" % (ch, token[:15]))
            else:
                _dbg("[simba] [%s] token FALHOU — origin_url JWT pode ter expirado" % ch)
        except Exception as e:
            _dbg("[simba] [%s] erro: %s" % (ch, e))


def _recapture_simba_origins():
    """
    Reabre o Playwright com os cookies salvos (sem novo login) e
    re-navega apenas pelos canais cdnsimba para obter JWTs frescos.
    """
    global _session_cookies
    if not _session_cookies:
        _dbg("[simba] sem cookies de sessao, aguardando captura inicial…")
        return

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with lock:
        simba_chs = {
            ch: info for ch, info in streams.items()
            if info.get("origin_url") and "cdnsimba" in info.get("origin_url", "")
        }
    if not simba_chs:
        return

    _dbg("[simba] Recapturando JWTs para: %s" % list(simba_chs.keys()))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
            )
            ctx  = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
            # Restaura cookies da sessao (ja logado)
            ctx.add_cookies(_session_cookies)
            page = ctx.new_page()

            for ch, info in simba_chs.items():
                ch_url    = "%s/Live/LiveEvent/%s?groupId=%s" % (
                    BASE_URL, CHANNELS[ch]["event_id"], CHANNELS[ch]["group_id"])
                captured  = {}

                def _on_req(request, _c=captured, _ch=ch):
                    url = request.url
                    if ("cdnsimba" in url or "brasil.cdnsimba" in url) and                        ("index.m3u8" in url or "master.m3u8" in url) and                        not _c.get("origin_url"):
                        _c["origin_url"] = url
                        _dbg("[simba] [%s] novo JWT capturado" % _ch.upper())

                page.on("request", _on_req)
                try:
                    page.goto(ch_url, wait_until="domcontentloaded", timeout=15000)
                except PWTimeout:
                    pass
                # Aguarda até 6s pelo JWT
                for _ in range(12):
                    if captured.get("origin_url"):
                        break
                    page.wait_for_timeout(500)
                page.remove_listener("request", _on_req)

                if captured.get("origin_url"):
                    with lock:
                        if ch in streams:
                            streams[ch]["origin_url"] = captured["origin_url"]
                    # Invalida cache antigo para forçar token fresco
                    with _simba_lock:
                        _simba_token_cache.pop(captured["origin_url"], None)
                    _dbg("[simba] [%s] origin_url atualizada" % ch.upper())
                else:
                    _dbg("[simba] [%s] JWT NAO capturado, sessao pode ter expirado" % ch.upper())

            browser.close()
    except Exception:
        _dbg("[simba] ERRO na recaptura:\n" + traceback.format_exc())

    # Aquece tokens com os novos JWTs
    _warmup_simba_tokens()


def _simba_loop():
    """
    A cada 5 minutos:
      1. Re-captura JWTs frescos dos canais cdnsimba via Playwright (sem login)
      2. Aquece o cache de bpk-tokens com os novos JWTs
    """
    while True:
        time.sleep(300)   # 5 minutos
        try:
            _recapture_simba_origins()
        except Exception:
            _dbg("[simba] ERRO no loop:\n" + traceback.format_exc())


def _refresh_loop():
    while True:
        try:
            fetch_streams()
            # Aquece cache cdnsimba imediatamente após captura
            _warmup_simba_tokens()
        except Exception:
            _dbg("ERRO CRITICO:\n" + traceback.format_exc())
        _dbg("Proxima renovacao em %d min" % (REFRESH_INTERVAL // 60))
        time.sleep(REFRESH_INTERVAL)


# ── REESCRITA M3U8 ────────────────────────────────────────────────────────────
def _proxy_url(url, ch=""):
    base = "/proxy?u=%s" % quote(url, safe="")
    if ch:
        base += "&_ch=%s" % quote(ch, safe="")
    return base


def _abs_url(url, base_root):
    """Converte URL relativa em absoluta."""
    if url.startswith("http"):
        return url
    return base_root + "/" + url.lstrip("/")


def _rewrite_m3u8(content, base_url, ch=""):
    """
    Reescreve todas as URLs de uma playlist HLS para passar pelo proxy local.
    Trata:
      - linhas de URL (após #EXT-X-STREAM-INF etc.)
      - URI= dentro de #EXT-X-MEDIA, #EXT-X-I-FRAME-STREAM-INF e similares
    O parametro ch (canal) e embutido nas URLs para que o proxy saiba
    qual origin_url usar ao renovar tokens cdnsimba.
    """
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
    """
    Lê o master.m3u8 e retorna dict de qualidade -> URL absoluta da variante.
    Qualidades: fhd (1080p), hd (720p), sd (melhor abaixo de 720p)
    """
    parsed    = urlparse(base_url)
    base_root = "%s://%s" % (parsed.scheme, parsed.netloc)
    variants  = {}
    lines     = content.splitlines()

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        # Proxima linha nao vazia e nao comentario = URL da variante
        for j in range(i + 1, len(lines)):
            url_line = lines[j].strip()
            if not url_line or url_line.startswith("#"):
                continue
            # URL absoluta
            if url_line.startswith("http"):
                abs_url = url_line
            else:
                abs_url = base_root + "/" + url_line.lstrip("/")
            # Resolucao
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

    # Fallbacks: garante que sempre existam fhd/hd/sd
    if "fhd" not in variants and "hd" in variants:
        variants["fhd"] = variants["hd"]
    if "hd" not in variants and "fhd" in variants:
        variants["hd"] = variants["fhd"]
    if "sd" not in variants:
        variants["sd"] = variants.get("low") or variants.get("hd") or variants.get("fhd", "")

    return {k: v for k, v in variants.items() if k in ("fhd", "hd", "sd") and v}


def _get_fresh_simba_token(origin_url):
    """
    Faz GET na origin_url do cdnsimba (sem seguir redirect),
    extrai o token fresco do header Location e cacheia por 45s.
    Retorna (cache_base, fresh_token) ou (None, None) se falhar.
    """
    with _simba_lock:
        cached = _simba_token_cache.get(origin_url)
        if cached and (time.time() - cached["ts"]) < 45:
            return cached["cache_base"], cached["token"]

    try:
        r = requests.get(origin_url, headers={"User-Agent": UA,
                         "Referer": BASE_URL + "/", "Origin": BASE_URL},
                         timeout=10, allow_redirects=False)
        location = r.headers.get("Location", "")
        # Location: https://cache01sp.cdnsimba.com.br:443/bpk-token/TOKEN/path
        m = re.match(r"(https://[^/]+)/bpk-token/([^/]+)/", location)
        if not m:
            return None, None
        cache_base  = m.group(1)   # https://cache01sp.cdnsimba.com.br:443
        fresh_token = m.group(2)   # 2ac@xxxxx
        with _simba_lock:
            _simba_token_cache[origin_url] = {
                "cache_base": cache_base,
                "token":      fresh_token,
                "ts":         time.time(),
            }
        _dbg("[simba] token fresco: %s…" % fresh_token[:20])
        return cache_base, fresh_token
    except Exception as e:
        _dbg("[simba] erro ao renovar token: %s" % e)
        return None, None


def _simba_url_with_fresh_token(orig_cdnsimba_url, origin_url):
    """
    Dado uma URL cdnsimba com token velho (ex: cache01sp.../bpk-token/OLD/linear/...),
    retorna a mesma URL com token fresco.
    """
    cache_base, fresh_token = _get_fresh_simba_token(origin_url)
    if not cache_base or not fresh_token:
        return orig_cdnsimba_url  # fallback: usa URL velha

    # Extrai o path lógico após o token: /linear/hls/pa/event/...
    m = re.search(r"/bpk-token/[^/]+(/.*)", orig_cdnsimba_url)
    if not m:
        return orig_cdnsimba_url
    logical_path = m.group(1)
    return "%s/bpk-token/%s%s" % (cache_base, fresh_token, logical_path)


def _fetch_master_live(info):
    """
    Re-busca o master.m3u8 ao vivo e retorna (body, master_url).
    - Canais cdnsimba: re-faz GET na origin_url (pre-redirect) para obter token fresco.
    - Canais Akamai:   re-faz GET com cookie hdntl.
    """
    origin_url = info.get("origin_url", "")
    master_url = info["master_url"]
    hdntl_val  = info.get("hdntl", "")

    if origin_url:
        # cdnsimba: pega token fresco e monta URL com ele
        cache_base, fresh_token = _get_fresh_simba_token(origin_url)
        if cache_base and fresh_token:
            # Extrai path base do master (ex: /bpk-tv/RecordMANSRT/default/index.m3u8)
            m = re.search(r"/bpk-token/[^/]+(/.*)", info.get("master_url", ""))
            if m:
                fresh_url = "%s/bpk-token/%s%s" % (cache_base, fresh_token, m.group(1))
            else:
                fresh_url = info["master_url"]
        else:
            fresh_url = info["master_url"]
        r = requests.get(fresh_url, headers={"User-Agent": UA}, timeout=10)
        r.raise_for_status()
        return r.text, fresh_url
    else:
        # Akamai: usa hdntl cookie
        r = _make_akamai_request(master_url, hdntl_val)
        r.raise_for_status()
        return r.text, master_url


# ── FLASK ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    host = req.host.split(":")[0]
    QUALITY_LABEL = {"fhd": "Full HD 1080p", "hd": "HD 720p", "sd": "SD 480p"}
    rows = []
    for ch in CHANNELS:
        with lock:
            info = streams.get(ch, {})
        status   = "OK" if ch in streams else "Aguardando"
        variants = info.get("variants", {})

        # Linha principal do canal
        rows.append(
            "<tr>"
            "<td rowspan='4'><b>%s</b><br><small>%s</small></td>"
            "<td>Todos</td>"
            "<td><code>http://%s:%d/channel/%s</code></td>"
            "<td>%s</td>"
            "</tr>" % (ch.upper(), CHANNELS[ch]["name"], host, PORT, ch, status)
        )
        # Linhas de qualidade
        for q in ("fhd", "hd", "sd"):
            q_status = "OK" if q in variants else "N/A"
            rows.append(
                "<tr>"
                "<td>%s</td>"
                "<td><code>http://%s:%d/channel/%s/%s</code></td>"
                "<td>%s</td>"
                "</tr>" % (QUALITY_LABEL[q], host, PORT, ch, q, q_status)
            )

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>RecordPlus Proxy</title></head>"
        "<body style='font-family:sans-serif;padding:2em'>"
        "<h2>RecordPlus HLS Proxy v3</h2>"
        "<p>Use os links abaixo no VLC: <b>Media &rarr; Abrir fluxo de rede</b></p>"
        "<table border='1' cellpadding='8' cellspacing='0'>"
        "<tr><th>Canal</th><th>Qualidade</th><th>URL para VLC</th><th>Status</th></tr>"
        + "".join(rows) +
        "</table><br>"
        "<a href='/playlist.m3u' download='recordplus.m3u'>"
        "<button style='margin-right:12px;padding:8px 18px;font-size:14px;background:#e53935;color:#fff;border:none;border-radius:4px;cursor:pointer'>"
        "&#11123; Baixar playlist M3U</button></a>"
        "<a href='/debug'>Log de diagnostico</a>"
        "</body></html>"
    )


@app.route("/playlist.m3u")
def playlist_m3u():
    host = req.host.split(":")[0]
    QUALITY_LABEL = {"fhd": "Full HD 1080p", "hd": "HD 720p", "sd": "SD 480p"}
    lines = ["#EXTM3U"]
    with lock:
        snap = dict(streams)
    for ch, info in snap.items():
        ch_name  = CHANNELS[ch]["name"]
        variants = info.get("variants", {})
        # Entrada "todos" (master com selecao automatica)
        lines.append(
            '#EXTINF:-1 tvg-id="%s" tvg-name="%s" group-title="RecordPlus",%s' % (
                ch, ch_name, ch_name)
        )
        lines.append("http://%s:%d/channel/%s" % (host, PORT, ch))
        # Entradas por qualidade
        for q in ("fhd", "hd", "sd"):
            if q not in variants:
                continue
            label = "%s (%s)" % (ch_name, QUALITY_LABEL[q])
            lines.append(
                '#EXTINF:-1 tvg-id="%s_%s" tvg-name="%s" group-title="RecordPlus %s",%s' % (
                    ch, q, label, QUALITY_LABEL[q], label)
            )
            lines.append("http://%s:%d/channel/%s/%s" % (host, PORT, ch, q))
    content = "\n".join(lines) + "\n"
    return Response(
        content,
        mimetype="audio/x-mpegurl",
        headers={"Content-Disposition": "attachment; filename=recordplus.m3u"},
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

    # Sempre re-busca o master ao vivo para garantir tokens frescos
    _dbg("[%s] Buscando master ao vivo…" % ch)
    try:
        body, live_url = _fetch_master_live(info)
    except Exception as e:
        _dbg("[%s] Erro upstream: %s — disparando renovacao" % (ch, e))
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

    variants    = info.get("variants", {})
    variant_url = variants.get(quality)

    if not variant_url:
        return Response(
            "Qualidade '%s' nao disponivel para o canal %s." % (quality, ch),
            status=404, mimetype="text/plain")

    # Re-busca master ao vivo para obter variantes com tokens frescos
    hdntl_val = info.get("hdntl", "")
    try:
        master_body_live, live_url = _fetch_master_live(info)
        fresh_variants = _parse_variants(master_body_live, live_url)
        variant_url = fresh_variants.get(quality) or variant_url
    except Exception as e:
        _dbg("[%s/%s] Erro ao re-buscar master: %s" % (ch, quality, e))
        # Continua com variant_url cacheada como fallback

    try:
        r = _make_akamai_request(variant_url, hdntl_val) if "akamai" in variant_url             else requests.get(variant_url, headers={"User-Agent": UA}, timeout=12)
        r.raise_for_status()
    except Exception as e:
        _dbg("[%s/%s] Erro upstream: %s — disparando renovacao" % (ch, quality, e))
        _trigger_refresh()
        return Response(
            "Stream temporariamente indisponivel. Renovando tokens, tente em 15s.",
            status=503, mimetype="text/plain")

    content = _rewrite_m3u8(r.text, variant_url, ch)
    return Response(content, mimetype="application/x-mpegURL",
                    headers={"Cache-Control": "no-cache, no-store"})


@app.route("/proxy")
def proxy():
    url = unquote(req.args.get("u", ""))
    ch  = req.args.get("_ch", "")
    if not url:
        abort(400)

    hdntl_val = _hdntl_for_url(url)
    if not hdntl_val:
        with lock:
            vals = list(akamai_cookies.values())
        hdntl_val = vals[0] if vals else None

    # Para cdnsimba: renova token usando o canal exato (_ch param)
    if "cdnsimba" in url and not hdntl_val:
        origin_url = None
        if ch:
            # Usa o canal que gerou esta URL — token correto garantido
            with lock:
                origin_url = streams.get(ch, {}).get("origin_url", "")
        if not origin_url:
            # Fallback por domínio
            req_domain = urlparse(url).netloc
            with lock:
                for v in streams.values():
                    vo = v.get("origin_url", "")
                    if not vo or "cdnsimba" not in vo:
                        continue
                    cached = _simba_token_cache.get(vo)
                    if cached and req_domain in cached.get("cache_base", ""):
                        origin_url = vo
                        break
                if not origin_url:
                    origin_url = next(
                        (v.get("origin_url") for v in streams.values()
                         if "cdnsimba" in v.get("origin_url", "")), None
                    )
        if origin_url:
            fresh_url = _simba_url_with_fresh_token(url, origin_url)
            if fresh_url != url:
                url = fresh_url

    try:
        if hdntl_val:
            up = _make_akamai_request(url, hdntl_val, stream=True)
        else:
            up = requests.get(url, headers={"User-Agent": UA,
                              "Referer": BASE_URL + "/", "Origin": BASE_URL},
                              stream=True, timeout=15, allow_redirects=True)
        up.raise_for_status()
    except Exception as e:
        _dbg("proxy err: %s -> %s" % (e, url[:60]), "warning")
        _trigger_refresh()
        abort(502)

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
    print("  RecordPlus Proxy v3  |  porta %d" % PORT, flush=True)
    print("=" * 50, flush=True)

    t = threading.Thread(target=_refresh_loop, daemon=True, name="refresh")
    t.start()
    ts = threading.Thread(target=_simba_loop, daemon=True, name="simba-token")
    ts.start()

    # Flask sobe imediatamente — captura roda em background
    print("Flask subindo agora. Streams chegarao em ~2min.", flush=True)
    sys.stdout.flush()

    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
