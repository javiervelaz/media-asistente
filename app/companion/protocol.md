# Protocolo del compañero (v1)

Charly → pantalla por WebSocket en `/companion/ws`. JSON de texto y un mensaje por frame. Lo consumen igual el simulador (`/companion/preview`) y la placa Waveshare ESP32-S3-Touch-LCD-4B (A1).

## Autenticación
- **Placa:** header `x-api-key: <API_KEY>` en el handshake.
- **Navegador:** primer mensaje `{"type":"hello","key":"<API_KEY>","client":"preview"}` dentro de los 5 s.
- Si falla, se cierra con el código **4401**. El cliente no debe reintentar en loop.

## `state`: lo que hay que dibujar
Se manda al conectar, cada vez que cambia algo que se ve y como latido cada 15 s.

```json
{
  "v": 1, "type": "state", "seq": 42, "server_ts": 1793560000.12,
  "mode": "now_playing",          // now_playing | idle | offline
  "mood": "encantado",            // ver app/companion/mood.py
  "event": "track_changed",       // o null. Disparador de animación, no estado.
  "track": {
    "youtube_id": "abc123",
    "title": "Gimme Shelter", "artist": "The Rolling Stones",
    "album": "Let It Bleed", "year": 1969,
    "cover": "https://coverartarchive.org/release-group/<rg>/front-500",
    "cover_source": "caa",        // caa | youtube | null
    "from_vinyl": true,           // el disco está en tu colección
    "artist_in_collection": true,
    "rationale": "…"              // por qué lo eligió el curador, si lo hay
  },
  "playback": { "paused": false, "position_sec": 95.2, "duration_sec": 271.0, "volume": 70 }
}
```
- `track` y `playback` son `null` fuera de `now_playing`.
- **La posición la interpola el cliente** con su propio reloj desde que llegó el mensaje. `server_ts` sirve para logs, no para sincronizar: el reloj de la Pi y el del cliente no tienen por qué coincidir.

## `message`: texto para mostrar (A3)
```json
{ "v": 1, "type": "message", "seq": 43, "server_ts": 1793560001.0, "text": "Pongo algo de Spinetta.", "ttl_s": 20 }
```
Se muestra como globo durante `ttl_s` segundos.

## `control`: del cliente a Charly
El touch de la placa y los botones del simulador mandan, por el mismo WebSocket:
```json
{ "type": "control", "action": "next" }
```
Acciones: `toggle` (play/pausa según el estado), `play`, `pause`, `next`, `prev`, `stop`, `volume_up`, `volume_down` (±5).

Corren **el mismo código que `/control/*`**: cancelan el fade y `next`/`prev` registran el avance en `play_history`, igual que desde Telegram. Charly responde solo a ese cliente:
```json
{ "type": "ack", "action": "next", "ok": true }
{ "type": "ack", "action": "next", "ok": false, "error": "mpv no responde" }
```
El nuevo `state` llega enseguida (~150 ms), sin esperar el próximo poll.

**Gestos de referencia** (simulador y placa): tocar = `toggle`, deslizar ← = `next`, → = `prev`, ↑ = `volume_up`, ↓ = `volume_down`.

## Compatibilidad
- Campos nuevos se agregan sin subir `v`. Un cliente **ignora lo que no conoce**.
- `v` sube solo si cambia el significado de un campo existente.
