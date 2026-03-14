#!/bin/bash
set -e

echo ""
echo "=========================================="
echo "   PAUDRONIX GT - INSTALADOR AUTOMATICO"
echo "=========================================="
echo ""

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Ejecuta este script como root"
    echo "Usa: sudo bash install.sh"
    exit 1
fi

APP_DIR="/opt/otppro"
DB_NAME="otp_db"
DB_USER="otp_user"
DB_PASS=$(openssl rand -hex 12)
SECRET_KEY=$(openssl rand -hex 32)
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo "[1/8] Instalando dependencias del sistema..."
apt update -qq
apt install -y -qq python3 python3-pip python3-venv postgresql postgresql-contrib nginx curl > /dev/null 2>&1
echo "  OK"

echo "[2/8] Configurando PostgreSQL..."
systemctl start postgresql
systemctl enable postgresql > /dev/null 2>&1

sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';" > /dev/null 2>&1

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" > /dev/null 2>&1

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" > /dev/null 2>&1
sudo -u postgres psql -d ${DB_NAME} -c "GRANT ALL ON SCHEMA public TO ${DB_USER};" > /dev/null 2>&1
echo "  OK"

echo "[3/8] Obteniendo archivos del proyecto..."
mkdir -p ${APP_DIR}/static/uploads
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "${SCRIPT_DIR}/app.py" ]; then
    cp "${SCRIPT_DIR}/app.py" ${APP_DIR}/
    cp "${SCRIPT_DIR}/requirements.txt" ${APP_DIR}/
    cp -r "${SCRIPT_DIR}/templates" ${APP_DIR}/
    if [ -d "${SCRIPT_DIR}/static" ]; then
        cp -r "${SCRIPT_DIR}/static/"* ${APP_DIR}/static/ 2>/dev/null || true
    fi
else
    echo "  ERROR: No se encontro app.py en ${SCRIPT_DIR}"
    echo "  Asegurate de ejecutar desde la carpeta webopt del proyecto"
    exit 1
fi
mkdir -p ${APP_DIR}/static/uploads
echo "  OK"

echo "[4/8] Creando entorno virtual e instalando paquetes..."
python3 -m venv ${APP_DIR}/venv
${APP_DIR}/venv/bin/pip install --upgrade pip setuptools wheel -q > /dev/null 2>&1
${APP_DIR}/venv/bin/pip install -r ${APP_DIR}/requirements.txt -q > /dev/null 2>&1
echo "  OK"

echo "[5/8] Creando archivo de configuracion..."
cat > ${APP_DIR}/.env << ENVFILE
DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}
SECRET_KEY=${SECRET_KEY}
ADMIN_USER=paudronixGt20p
ADMIN_PASS=paudronixADM20a
ENVFILE
chmod 600 ${APP_DIR}/.env
echo "  OK"

echo "[6/8] Creando servicio systemd..."
cat > /etc/systemd/system/otppro.service << SERVICEFILE
[Unit]
Description=Paudronix GT - Sistema de OTP
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
Environment=PATH=${APP_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=${APP_DIR}/.env
ExecStartPre=/bin/bash -c '/usr/bin/fuser -k 5000/tcp 2>/dev/null || true'
ExecStart=${APP_DIR}/venv/bin/python app.py
Restart=always
RestartSec=3
StartLimitBurst=0
MemoryMax=200M
MemoryHigh=150M
CPUQuota=40%
OOMPolicy=stop

[Install]
WantedBy=multi-user.target
SERVICEFILE

systemctl daemon-reload
systemctl enable otppro > /dev/null 2>&1
systemctl restart otppro
echo "  OK"

echo "[7/8] Configurando Nginx..."
cat > /etc/nginx/sites-available/otppro << NGINXFILE
server {
    listen 80;
    server_name ${SERVER_IP};

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }

    location /static/ {
        alias ${APP_DIR}/static/;
        expires 7d;
    }
}
NGINXFILE

ln -sf /etc/nginx/sites-available/otppro /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t > /dev/null 2>&1
systemctl restart nginx
echo "  OK"

echo "[8/8] Configurando firewall..."
ufw allow 22 > /dev/null 2>&1
ufw allow 80 > /dev/null 2>&1
ufw allow 443 > /dev/null 2>&1
echo "y" | ufw enable > /dev/null 2>&1
echo "  OK"

sleep 3

STATUS=$(systemctl is-active otppro)

echo ""
echo "=========================================="
echo "   INSTALACION COMPLETADA"
echo "=========================================="
echo ""
echo "  Estado del servicio: ${STATUS}"
echo ""
echo "  Abre en tu navegador: http://${SERVER_IP}"
echo ""
echo "------------------------------------------"
echo "  DATOS PARA ENTRAR AL PANEL ADMIN:"
echo ""
echo "    Usuario:   paudronixGt20p"
echo "    Contrasena: paudronixADM20a"
echo ""
echo "  (Los otros datos del .env son internos"
echo "   del sistema, NO se usan para login)"
echo "------------------------------------------"
echo ""
echo "  Comandos utiles:"
echo "    Ver estado:  sudo systemctl status otppro"
echo "    Reiniciar:   sudo systemctl restart otppro"
echo "    Ver logs:    sudo journalctl -u otppro -f"
echo ""
echo "=========================================="
echo ""
