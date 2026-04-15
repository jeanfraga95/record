#!/usr/bin/env bash
# ============================================================
#  record Proxy — Instalador para Ubuntu 22.04 (ARM/x64)
#  Execute como root: sudo bash install.sh
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/record-proxy"
SERVICE_NAME="record-proxy"
PYTHON="python3"
GITHUB_RAW="https://raw.githubusercontent.com/jeanfraga95/record/refs/heads/main/proxy.py"

echo "========================================================"
echo "  Record HLS Proxy — Instalação"
echo "========================================================"

# ── dependências do sistema ──────────────────────────────────
echo "[1/6] Atualizando pacotes do sistema…"
apt-get update -y
apt-get install -y python3 python3-pip python3-venv \
    curl wget git \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    fonts-liberation xdg-utils

# ── diretório de instalação ──────────────────────────────────
echo "[2/6] Criando diretório $INSTALL_DIR…"
mkdir -p "$INSTALL_DIR"

echo "  Baixando proxy.py do GitHub…"
curl -fsSL "$GITHUB_RAW" -o "$INSTALL_DIR/proxy.py"
echo "  ✓ proxy.py baixado com sucesso."

# ── ambiente Python virtual ──────────────────────────────────
echo "[3/6] Criando ambiente virtual Python…"
$PYTHON -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

pip install --upgrade pip
pip install flask requests playwright

# ── Chromium via Playwright ──────────────────────────────────
echo "[4/6] Instalando Chromium (Playwright)…"
python -m playwright install chromium
python -m playwright install-deps chromium

deactivate

# ── serviço systemd ──────────────────────────────────────────
echo "[5/6] Criando serviço systemd…"
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=R
record HLS Proxy
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
systemctl enable  ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

# ── abre porta no firewall (se ufw ativo) ──────────────────
echo "[6/6] Abrindo porta 8888 no firewall…"
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 8888/tcp
    echo "  ufw: porta 8888 liberada."
else
    echo "  ufw não está ativo — verifique as regras de iptables/oci manualmente."
fi

# ── resultado ─────────────────────────────────────────────────
PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "<SEU_IP>")
echo ""
echo "========================================================"
echo "  Instalação concluída!"
echo ""
echo "  O proxy inicia em background. Aguarde ~30 s para"
echo "  o login e captura dos streams ficarem prontos."
echo ""
echo "  URLs para VLC (Mídia → Abrir fluxo de rede):"
echo "    http://${PUBLIC_IP}:8888/channel/sp"
echo "    http://${PUBLIC_IP}:8888/channel/rio"
echo "    http://${PUBLIC_IP}:8888/channel/minas"
echo ""
echo "  Painel web:"
echo "    http://${PUBLIC_IP}:8888/"
echo ""
echo "  Logs:"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo "    tail -f /var/log/record-proxy.log"
echo "========================================================"
