#!/bin/sh
# Écran virtuel + accès noVNC (connexion MFA manuelle dans le MÊME navigateur que le scraper),
# puis API HTTP. Sans KB_BROWSER_VNC_PASSWORD, noVNC n'est pas démarré (sécurisé par défaut).
set -e

Xvfb :99 -screen 0 1366x900x24 -nolisten tcp &
sleep 1

if [ -n "$KB_BROWSER_VNC_PASSWORD" ]; then
    x11vnc -storepasswd "$KB_BROWSER_VNC_PASSWORD" /tmp/vncpass >/dev/null
    x11vnc -display :99 -forever -shared -localhost -rfbauth /tmp/vncpass -rfbport 5900 -quiet &
    websockify --web /usr/share/novnc 6080 localhost:5900 &
    echo "noVNC disponible sur le port 6080 (connexion/MFA manuelle)."
else
    echo "KB_BROWSER_VNC_PASSWORD absent : noVNC désactivé, reconnexion MFA impossible depuis le navigateur."
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8090
