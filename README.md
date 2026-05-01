# Media Asistente

Asistente de música con IA para Raspberry Pi. Genera playlists desde prompts en lenguaje natural usando Claude, las busca en YouTube Music, y las reproduce vía mpv con audio Bluetooth y video HDMI.

## Setup en la Pi

```bash
git clone https://github.com/TU_USER/media-asistente.git
cd media-asistente
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# editá .env con tus claves reales
```

## Correr

Asegurate que mpv está corriendo con socket IPC:
```bash
systemd-run --user --unit=mpv-player \
    mpv --no-video --idle=yes --keep-open=always --no-terminal \
    --input-ipc-server=/tmp/mpvsocket
```

Después la API:
```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Test

```bash
curl -X POST http://localhost:8000/playlist \
  -H "X-API-Key: tu_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "rock alternativo de los 90s"}'
```