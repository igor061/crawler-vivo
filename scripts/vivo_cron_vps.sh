#!/usr/bin/env bash
# Download diario das faturas Vivo Empresas (crawler-vivo) no contabo001.
#
# Caminho VPS valido: Camoufox 0.5.x + fingerprint preset macOS real (v150)
# via proxy residencial do Mac (tinyproxy 10.202.0.3:8888).
# Pre-requisitos: Mac ligado com tinyproxy no ar; xvfb + libs Mesa instaladas.
#
# Dependencia externa importante: se o Mac estiver desligado, aborta cedo.

PROJ="$HOME/crawler-vivo"
LOG_DIR="$PROJ/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/vivo-cron-$STAMP.log"
mkdir -p "$LOG_DIR"

log() { echo "[cron] $*" >> "$LOG_FILE"; }

PROXY_URL="${VIVO_PROXY:-http://10.202.0.3:8888}"

log "Inicio: $(date '+%Y-%m-%d %H:%M:%S %Z')"

# Pre-checa o proxy residencial do Mac (evita 3 retries contra proxy morto).
if ! curl -s -m 10 -x "$PROXY_URL" -o /dev/null https://api.ipify.org; then
    log "FALHA: proxy residencial $PROXY_URL inacessivel (Mac ligado/tinyproxy no ar?). Abortando."
    exit 1
fi
log "Proxy $PROXY_URL OK, IP de saida: $(curl -s -m 10 -x "$PROXY_URL" https://api.ipify.org)"

cd "$PROJ" || { log "FALHA: cd $PROJ"; exit 1; }

set -a
[ -f .env ] && source .env
set +a

export VIVO_PROXY="$PROXY_URL"
export VIVO_CAMOUFOX_OS=macos
export VIVO_LOGIN_RETRIES=3
export VIVO_LOGIN_RETRY_BACKOFF=300

log "Executando vivo_movel.py (camoufox, mode virtual, retries=3)"
"$PROJ/.venv/bin/python" "$PROJ/vivo_movel.py" --engine camoufox --mode virtual \
    >> "$LOG_FILE" 2>&1
RC=$?
log "exit=$RC"

# Rotaciona logs, mantendo os 14 mais recentes.
ls -1t "$LOG_DIR"/vivo-cron-*.log 2>/dev/null | tail -n +15 | xargs -r rm -f
exit "$RC"
