"""A0 — compañero de escritorio.

Charly piensa, la caja dibuja. Este paquete arma el "view state" (que suena,
con que tapa, de que humor esta el companero) y lo empuja por WebSocket a
quien este mirando: la placa Waveshare de 4" o el simulador del navegador
(`/companion/preview`). Los dos consumen exactamente el mismo protocolo, asi
que lo que se ve en el simulador es lo que va a dibujar el hardware.

Protocolo: ver `protocol.md` en esta carpeta.
"""
