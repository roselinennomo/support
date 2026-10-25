# Navigateur Chromium (Playwright) pour la recherche dans la KB constructeur.
# Base Docker Hub + Chromium téléchargé par Playwright : aucune dépendance à mcr.microsoft.com
# (souvent bloqué par les proxys d'entreprise). La version de `playwright` est fixée dans requirements.txt.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright DISPLAY=:99 KB_PROFILE_DIR=/data/profile

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data/profile"]
EXPOSE 8090 6080
ENTRYPOINT ["/entrypoint.sh"]
