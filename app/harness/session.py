"""Estado de sesion para resolver anaforas sin mandar historial al modelo.

"Pone otro de ellos" no necesita los ultimos N turnos como contexto: necesita
un struct. El clasificador (H3) devuelve artista="$last" y el ejecutor lo
resuelve contra esto. Determinista, cero tokens, y no depende de que el modelo
se acuerde bien.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

TTL_MIN = 20
MAX_SESIONES = 200          # Pi 3B: 1 GB


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class SessionState:
    session_id: str
    room_id: str = "main"
    last_artist: str | None = None
    last_artist_mbid: str | None = None
    last_release_mbid: str | None = None
    last_playlist_id: str | None = None
    last_intent: str | None = None
    updated_at: datetime = field(default_factory=_now)

    def vigente(self, ttl_min: int = TTL_MIN) -> bool:
        return (_now() - self.updated_at).total_seconds() < ttl_min * 60

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
