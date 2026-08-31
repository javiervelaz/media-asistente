"""Estado de sesion para resolver anaforas sin mandar historial al modelo.

"Pone otro de ellos" no necesita los ultimos N turnos como contexto: necesita
un struct. El clasificador (H3) devuelve artista="$last" y el ejecutor lo
resuelve contra esto. Determinista, cero tokens, y no depende de que el modelo
se acuerde bien.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

TTL_MIN = 20
TTL_OFERTA_MIN = 5      # una oferta vieja sorprende: "dale" tiene que ser de recien
MAX_SESIONES = 200          # Pi 3B: 1 GB


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Oferta:
    """Una accion que una consulta dejo ofrecida.

    Es lo que hace que "efemerides" -> "dale" cueste cero tokens en los dos
    turnos: el segundo no necesita entender nada, solo ejecutar lo que el
    primero ya resolvio.
    """
    intent: str
    slots: dict[str, Any] = field(default_factory=dict)
    etiqueta: str = ""
    creada: datetime = field(default_factory=_now)

    def vigente(self) -> bool:
        return (_now() - self.creada).total_seconds() < TTL_OFERTA_MIN * 60


@dataclass(slots=True)
class SessionState:
    session_id: str
    room_id: str = "main"
    last_artist: str | None = None
    last_artist_mbid: str | None = None
    last_release_mbid: str | None = None
    last_playlist_id: str | None = None
    last_intent: str | None = None
    oferta: Oferta | None = None
    updated_at: datetime = field(default_factory=_now)

    def vigente(self, ttl_min: int = TTL_MIN) -> bool:
        return (_now() - self.updated_at).total_seconds() < ttl_min * 60

    def ofrecer(self, intent: str, etiqueta: str, **slots) -> None:
        self.oferta = Oferta(intent=intent, slots=slots, etiqueta=etiqueta)

    def tomar_oferta(self) -> "Oferta | None":
        """Devuelve la oferta y la consume: un 'dale' no se ejecuta dos veces."""
        o = self.oferta
        self.oferta = None
        return o if (o and o.vigente()) else None

    def tocar(self, **kw) -> None:
        for k, v in kw.items():
            if v is not None and hasattr(self, k):
                setattr(self, k, v)
        self.updated_at = _now()


_sesiones: "OrderedDict[str, SessionState]" = OrderedDict()


def get(session_id: str, room_id: str = "main") -> SessionState:
    st = _sesiones.get(session_id)
    if st is None or not st.vigente():
        st = SessionState(session_id=session_id, room_id=room_id)
        _sesiones[session_id] = st
    else:
        _sesiones.move_to_end(session_id)
    while len(_sesiones) > MAX_SESIONES:
        _sesiones.popitem(last=False)
    return st
