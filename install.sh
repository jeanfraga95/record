#!/usr/bin/env bash
# ============================================================
#  RecordPlus Proxy — Instalador com REINSTALAÇÃO AUTOMÁTICA
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/recordplus-proxy"
SERVICE_NAME="recordplus-proxy"
PYTHON="python3"
GITHUB_RAW="https://raw.githubusercontent.com/jeanfraga95/record/main/proxy.py"
PORT="8888"

echo "========================================================"
echo "  RecordPlus HLS Proxy — Instalação/Reinstalação"
echo "========================================================"

# ── limpeza de instalação anterior ───────────────────────────
echo "[0/6] Verificando instalação anterior…"

if systemctl list-units --full -all | grep -q "${SERVICE_NAME}.service"; then
    echo "  Parando serviço antigo…"
    systemctl stop ${SERVICE_NAME} || true
    systemctl disable ${SERVICE_NAME} || true
fi

if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    echo "  Removendo service antigo…"
    rm -f /etc/systemd/system/${SERVICE_NAME}.service
    systemctl daemon-reload
fi

if [ -d "$INSTALL_DIR" ]; then
    echo "  Removendo diretório antigo…"
    rm -rf "$INSTALL_DIR"
fi

# ── mata processo na porta ───────────────────────────────────
echo "  Verificando porta ${PORT}…"
if lsof -i :${PORT} >/dev/null 2>&1; then
    echo "  Porta em uso. Matando processo…"
    lsof -t -i :${PORT} | xargs -r kill -9
    sleep 1
    echo "  ✓ Porta liberada"
else
    echo "  Porta livre"
fi

# ── dependências ─────────────────────────────────────────────
echo "[1/6] Atualizando pacotes…"
apt-get update -y
apt-get install -y python3 python3-pip python3-venv \
    curl wget git lsof \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    fonts-liberation xdg-utils

# ── diretório ────────────────────────────────────────────────
echo "[2/6] Criando diretório…"
mkdir -p "$INSTALL_DIR"

echo "  Baixando proxy.py…"
curl -fsSL "$GITHUB_RAW" -o "$INSTALL_DIR/proxy.py"
echo "  ✓ Download concluído"

# ── venv ────────────────────────────────────────────────────
echo "[3/6] Criando ambiente Python…"
$PYTHON -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

pip install --upgrade pip
pip install flask requests playwright

# ── playwright ──────────────────────────────────────────────
echo "[4/6] Instalando Chromium…"
python -m playwright install chromium
python -m playwright install-deps chromium

deactivate

# ── systemd ─────────────────────────────────────────────────
echo "[5/6] Criando serviço…"
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=RecordPlus HLS Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/proxy.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

# ── firewall ────────────────────────────────────────────────
echo "[6/6] Liberando porta ${PORT}…"
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow ${PORT}/tcp
    echo "  ✓ Porta liberada no ufw"
else
    echo "  ufw não ativo"
fi

# ── final ───────────────────────────────────────────────────
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "<SEU_IP>")

echo ""
echo "========================================================"
echo "  Instalação concluída!"
echo ""
echo "  URLs:"
echo "    http://${PUBLIC_IP}:${PORT}/channel/sp"
echo "    http://${PUBLIC_IP}:${PORT}/channel/rio"
echo "    http://${PUBLIC_IP}:${PORT}/channel/minas"
echo ""
echo "  Painel:"
echo "    http://${PUBLIC_IP}:${PORT}/"
echo ""
echo "  Logs:"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo "========================================================"
