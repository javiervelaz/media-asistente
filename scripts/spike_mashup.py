"""Spike: mashup con grabaciones reales. La voz de un tema sobre la base de otro.

Pregunta que responde: ¿un mashup automatico suena a algo que quiero
escuchar, o hace falta un humano eligiendo cada corte? Si suena bien con
temas elegidos a mano, recien ahi tiene sentido que el curador elija el par.

Flujo:
  1. Audio de cada tema: archivo local, URL/ID de YouTube o "Artista - Tema"
     (busqueda con yt-dlp). Cache en mashups/_cache/.
  2. Analisis sobre la MEZCLA original (mas armonia que un stem aislado):
     BPM y grilla de beats (librosa), tonalidad (Krumhansl-Schmuckler).
  3. Demucs separa: voz del tema A, todo-menos-la-voz del tema B.
  4. La voz se estira al tempo de la base y se transpone a su tonalidad.
  5. Alineacion por grilla: la voz entra en un beat de la base, en multiplos
     de 4 beats (aproximacion a compas: librosa no da downbeats).
  6. Nivelacion RMS, mezcla, normalizacion a -1 dBFS, mp3 + sidecar json.

Corre en la notebook (Demucs necesita RAM y GPU; en la Pi 3B no entra).
El mp3 se lleva a la Pi con scp y se reproduce como cualquier otro.

Uso:
  python -m scripts.spike_mashup "The Beatles - Come Together" "Aerosmith - Walk This Way"
  python -m scripts.spike_mashup voz.mp3 base.mp3 --desfase 4 --gain-voz 2
  python -m scripts.spike_mashup "A - x" "B - y" --solo-analisis   # sin separar

Dependencias: scripts/requirements-mashup.txt + ffmpeg en el PATH.
Solo para escucha personal: son grabaciones con copyright.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("spike_mashup")
# librosa 1.0 avisa en cada time_stretch que n_fft/hop_length estan deprecados
# aunque no se los pasemos. Ruido puro en un log que se lee a ojo.
warnings.filterwarnings("ignore", category=FutureWarning, module="librosa")

SR = 44100                      # el samplerate de htdemucs
SALIDA = Path(os.environ.get("MASHUPS_DIR", "mashups"))
CACHE = SALIDA / "_cache"

NOTAS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Perfiles de Krumhansl-Kessler. Clasicos, imperfectos con rock muy
# distorsionado, y aun asi lo mejor que hay sin entrenar nada.
PERFIL_MAYOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                         2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
PERFIL_MENOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                         2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Mas alla de esto la voz se escucha procesada. No se bloquea: se avisa, porque
# el criterio final es el oido y no un umbral.
MAX_STRETCH = 0.12
MAX_SEMITONOS = 3


# === 1: audio ===============================================================

def slug(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")
    return s[:60] or "tema"


def clave(fuente: str) -> str:
    """Nombre de cache por fuente. Un archivo local usa su nombre, no la ruta:
    con la ruta recortada, dos archivos de la misma carpeta compartian cache y
    la base terminaba usando los stems de la voz. El hash corto desempata
    nombres iguales en carpetas distintas."""
    p = Path(fuente)
    legible = p.stem if p.is_file() else fuente
    h = hashlib.sha1(str(p.resolve() if p.is_file() else fuente)
                     .encode()).hexdigest()[:6]
    return f"{slug(legible)[:50]}-{h}"


def obtener_audio(fuente: str) -> Path:
    """Archivo local tal cual; si no, yt-dlp a wav en el cache."""
    p = Path(fuente)
    if p.is_file():
        return p

    CACHE.mkdir(parents=True, exist_ok=True)
    destino = CACHE / f"{clave(fuente)}.wav"
    if destino.is_file():
        logger.info("cache: %s", destino.name)
        return destino

    if not shutil.which("ffmpeg"):
        raise RuntimeError("falta ffmpeg en el PATH (yt-dlp lo necesita para "
                           "extraer el audio). En Windows: winget install ffmpeg")

    es_url = fuente.startswith("http") or re.fullmatch(r"[\w-]{11}", fuente)
    objetivo = fuente if es_url else f"ytsearch1:{fuente} official audio"
    cmd = [sys.executable, "-m", "yt_dlp", "-x", "--audio-format", "wav",
           "--no-playlist", "--no-progress",
           "-o", str(destino.with_suffix(".%(ext)s")), objetivo]
    # Mismo knob que usa app/tracks.py en la Pi. YouTube exige un runtime de
    # JS para descifrar firmas; sin el, yt-dlp falla con formatos vacios.
    if os.environ.get("YTDLP_JS_RUNTIME"):
        cmd[3:3] = ["--js-runtimes", os.environ["YTDLP_JS_RUNTIME"]]

    logger.info("bajando %r", fuente)
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not destino.is_file():
        raise RuntimeError(f"yt-dlp no pudo bajar {fuente!r}:\n"
                           f"{(r.stderr or r.stdout)[-600:]}\n"
                           "Alternativa: pasá el archivo local (por ejemplo "
                           "copiado del cache de la Pi).")
    return destino


def cargar(path: Path) -> np.ndarray:
    """Estereo (2, n) a 44.1 kHz. Un mono se duplica: Demucs espera 2 canales."""
    import librosa
    y, _ = librosa.load(str(path), sr=SR, mono=False)
    if y.ndim == 1:
        y = np.stack([y, y])
    return y.astype(np.float32)


# === 2: analisis ============================================================

def detectar_tonalidad(y_mono: np.ndarray) -> dict:
    import librosa
    chroma = librosa.feature.chroma_cqt(y=y_mono, sr=SR).mean(axis=1)
    mejor = None
    for modo, perfil in (("mayor", PERFIL_MAYOR), ("menor", PERFIL_MENOR)):
        for tonica in range(12):
            r = float(np.corrcoef(chroma, np.roll(perfil, tonica))[0, 1])
            if mejor is None or r > mejor["confianza"]:
                mejor = {"tonica": tonica, "modo": modo, "confianza": r}
    mejor["nombre"] = f"{NOTAS[mejor['tonica']]} {mejor['modo']}"
    return mejor


def analizar(y: np.ndarray) -> dict:
    import librosa
    mono = y.mean(axis=0)
    tempo, beats = librosa.beat.beat_track(y=mono, sr=SR, units="time")
    bpm = float(np.atleast_1d(tempo)[0])     # librosa >=0.10 devuelve array
    if len(beats) < 8:
        raise ValueError("no encontre una grilla de beats estable; ¿es un "
                         "tema sin percusion?")
    return {"bpm": round(bpm, 1), "beats": np.asarray(beats, dtype=float),
            "tonalidad": detectar_tonalidad(mono),
            "duracion": round(y.shape[1] / SR, 1)}


def ratio_tempo(bpm_voz: float, bpm_base: float) -> float:
    """Factor de velocidad para la voz. Considera doble y medio tiempo: 70 y
    140 BPM son el mismo groove, estirar al doble destruye la voz."""
    candidatos = [bpm_base / bpm_voz * f for f in (0.5, 1.0, 2.0)]
    return min(candidatos, key=lambda r: abs(np.log(r)))


def semitonos(voz: dict, base: dict) -> int:
    """Transposicion minima de la voz a la tonalidad de la base, en [-6, 5].
    Modos distintos se comparan por su relativo mayor (La menor == Do mayor):
    comparten notas, que es lo que importa para que no choquen."""
    def relativo_mayor(t):
        return (t["tonica"] + 3) % 12 if t["modo"] == "menor" else t["tonica"]
    d = relativo_mayor(base) - relativo_mayor(voz)
    return ((d + 6) % 12) - 6


# === 3: separacion ==========================================================

def separar(y: np.ndarray, dispositivo: str) -> dict[str, np.ndarray]:
    """htdemucs via su API: evita torchaudio.save, que en torchaudio >= 2.9
    exige torchcodec y rompe la CLI de demucs 4.0.1 en Windows."""
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    model = get_model("htdemucs")
    model.eval()
    wav = torch.from_numpy(y)
    ref = wav.mean(0)
    media, desvio = ref.mean(), ref.std()
    wav = (wav - media) / desvio
    with torch.no_grad():
        fuentes = apply_model(model, wav[None], device=dispositivo,
                              split=True, overlap=0.25, progress=True)[0]
    fuentes = fuentes * desvio + media
    stems = {n: fuentes[i].cpu().numpy() for i, n in enumerate(model.sources)}
    voz = stems.pop("vocals")
    return {"vocals": voz, "no_vocals": sum(stems.values())}


def separar_cacheado(nombre: str, y: np.ndarray, dispositivo: str) -> dict:
    """Separar un tema tarda minutos: se guarda por fuente y se reusa."""
    d = CACHE / "stems" / nombre
    archivos = {k: d / f"{k}.npy" for k in ("vocals", "no_vocals")}
    if all(p.is_file() for p in archivos.values()):
        logger.info("stems en cache: %s", nombre)
        return {k: np.load(p) for k, p in archivos.items()}
    logger.info("separando %s con htdemucs en %s...", nombre, dispositivo)
    stems = separar(y, dispositivo)
    d.mkdir(parents=True, exist_ok=True)
    for k, p in archivos.items():
        np.save(p, stems[k].astype(np.float32))
    return stems


# === 4: transformacion de la voz ============================================

def _rubberband() -> bool:
    if not shutil.which("rubberband"):
        return False
    try:
        import pyrubberband  # noqa: F401
        return True
    except ImportError:
        return False


def transformar(y: np.ndarray, ratio: float, n_semitonos: int) -> np.ndarray:
    """Tempo y tono por canal. Rubber Band si esta instalado (mucho mejor con
    voces); si no, el phase vocoder de librosa, que agrega algo de 'fase'."""
    if abs(ratio - 1) < 0.005 and n_semitonos == 0:
        return y
    if _rubberband():
        import pyrubberband as prb
        canales = [prb.pitch_shift(prb.time_stretch(c, SR, ratio), SR,
                                   n_semitonos) for c in y]
    else:
        import librosa
        canales = []
        for c in y:
            if abs(ratio - 1) >= 0.005:
                c = librosa.effects.time_stretch(c, rate=ratio)
            if n_semitonos:
                c = librosa.effects.pitch_shift(c, sr=SR, n_steps=n_semitonos)
            canales.append(c)
    largo = min(len(c) for c in canales)
    out = np.stack([c[:largo] for c in canales]).astype(np.float32)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


# === 5: alineacion ==========================================================

def entrada_de_voz(voz: np.ndarray, umbral_db: float = -30.0) -> float:
    """Primer instante con voz sostenida, en segundos. Umbral relativo al
    pico del stem: el sangrado de Demucs en la intro queda por debajo."""
    mono = np.abs(voz).mean(axis=0)
    hop = SR // 20                                  # ventanas de 50 ms
    n = len(mono) // hop
    if n == 0:
        return 0.0
    rms = np.sqrt((mono[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms / rms.max())
    activo = db > umbral_db
    # Medio segundo seguido arriba del umbral: un respiro o un click no cuentan.
    for i in range(n - 10):
        if activo[i:i + 10].all():
            return i * hop / SR
    return 0.0


def calcular_desfase(beats_voz: np.ndarray, beats_base: np.ndarray,
                     t_entrada: float, desfase_beats: int) -> tuple[float, dict]:
    """Segundos a desplazar la voz (positivo = la voz arranca despues).

    La voz entra donde entraba en su tema original, redondeada hacia atras a
    un bloque de 4 beats de SU grilla; en la base se busca el primer bloque de
    4 beats que no quede antes. Asi la voz no aparece en mitad de la intro ni
    a contratiempo. --desfase mueve la entrada de a beats.
    """
    j = max(int(np.searchsorted(beats_voz, t_entrada, side="right")) - 1, 0)
    j -= j % 4
    k = int(np.searchsorted(beats_base, beats_voz[j]))
    k += (-k) % 4
    k = int(np.clip(k + desfase_beats, 0, len(beats_base) - 1))
    return (float(beats_base[k] - beats_voz[j]),
            {"beat_voz": j, "beat_base": k,
             "entrada_voz_s": round(float(beats_voz[j]), 2),
             "entrada_en_base_s": round(float(beats_base[k]), 2)})


def desplazar(y: np.ndarray, segundos: float, largo: int) -> np.ndarray:
    n = round(segundos * SR)
    out = np.zeros((y.shape[0], largo), dtype=np.float32)
    if n >= 0:
        trozo = y[:, : max(largo - n, 0)]
        out[:, n:n + trozo.shape[1]] = trozo
    else:
        trozo = y[:, -n: -n + largo]
        out[:, : trozo.shape[1]] = trozo
    return out


# === 6: mezcla ==============================================================

def rms_activo(y: np.ndarray) -> float:
    """RMS solo de la parte que suena: los silencios de un stem de voz bajan
    el promedio y harian subir la voz de mas."""
    mono = np.abs(y).mean(axis=0)
    umbral = mono.max() * 0.02
    activos = mono[mono > umbral]
    return float(np.sqrt((activos ** 2).mean())) if activos.size else 1e-9


def mezclar(voz: np.ndarray, base: np.ndarray, gain_db: float,
            fade_s: float = 3.0) -> np.ndarray:
    voz = voz * (rms_activo(base) / rms_activo(voz)) * 10 ** (gain_db / 20)
    mix = base + voz
    n_fade = min(int(fade_s * SR), mix.shape[1])
    mix[:, -n_fade:] *= np.linspace(1, 0, n_fade, dtype=np.float32)
    pico = np.abs(mix).max()
    return (mix * (10 ** (-1 / 20) / pico)) if pico > 0 else mix


def exportar(mix: np.ndarray, base: Path) -> Path:
    import soundfile as sf
    base.parent.mkdir(parents=True, exist_ok=True)
    wav = base.with_suffix(".wav")
    sf.write(str(wav), mix.T, SR, subtype="PCM_16")
    if not shutil.which("ffmpeg"):
        logger.warning("sin ffmpeg: queda en wav")
        return wav
    mp3 = base.with_suffix(".mp3")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                        "-b:a", "256k", str(mp3)], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        logger.warning("ffmpeg fallo (%s): queda en wav", r.stderr[-200:])
        return wav
    wav.unlink()
    return mp3


# === main ===================================================================

def _resumen(nombre: str, a: dict) -> str:
    t = a["tonalidad"]
    return (f"{nombre}: {a['bpm']} BPM | {t['nombre']} "
            f"(confianza {t['confianza']:.2f}) | {a['duracion']}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("voz", help="tema del que se toma la voz")
    ap.add_argument("base", help="tema del que se toma todo menos la voz")
    ap.add_argument("--desfase", type=int, default=0,
                    help="mover la entrada de la voz N beats (puede ser negativo)")
    ap.add_argument("--gain-voz", type=float, default=0.0,
                    help="dB extra para la voz respecto de la base")
    ap.add_argument("--sin-transponer", action="store_true",
                    help="no tocar el tono de la voz")
    ap.add_argument("--duracion", type=float, default=None,
                    help="cortar el mashup a N segundos")
    ap.add_argument("--dispositivo", choices=("auto", "cuda", "cpu"),
                    default="auto")
    ap.add_argument("--solo-analisis", action="store_true",
                    help="BPM y tonalidad, sin separar ni mezclar")
    args = ap.parse_args()

    try:
        f_voz, f_base = obtener_audio(args.voz), obtener_audio(args.base)
        y_voz, y_base = cargar(f_voz), cargar(f_base)
        a_voz, a_base = analizar(y_voz), analizar(y_base)
        logger.info(_resumen("voz ", a_voz))
        logger.info(_resumen("base", a_base))

        ratio = ratio_tempo(a_voz["bpm"], a_base["bpm"])
        semi = 0 if args.sin_transponer else semitonos(a_voz["tonalidad"],
                                                       a_base["tonalidad"])
        avisos = []
        if abs(ratio - 1) > MAX_STRETCH:
            avisos.append(f"estiro la voz un {abs(ratio - 1):.0%}: se va a "
                          "notar. Probá otro par con tempos mas cercanos.")
        if abs(semi) > MAX_SEMITONOS:
            avisos.append(f"transpongo la voz {semi:+d} semitonos: va a sonar "
                          "procesada. Probá --sin-transponer o otro par.")
        if min(a_voz["tonalidad"]["confianza"],
               a_base["tonalidad"]["confianza"]) < 0.5:
            avisos.append("tonalidad detectada con poca confianza: si choca, "
                          "probá --sin-transponer.")
        logger.info("voz: tempo x%.3f, %+d semitonos", ratio, semi)
        for a in avisos:
            logger.warning(a)
        if args.solo_analisis:
            return 0

        dispositivo = args.dispositivo
        if dispositivo == "auto":
            import torch
            dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
            if dispositivo == "cpu":
                logger.warning("sin CUDA: Demucs en CPU tarda varios minutos "
                               "por tema (ver requirements-mashup.txt)")

        voz = separar_cacheado(clave(args.voz), y_voz, dispositivo)["vocals"]
        base = separar_cacheado(clave(args.base), y_base,
                                dispositivo)["no_vocals"]

        t_entrada = entrada_de_voz(voz)
        voz = transformar(voz, ratio, semi)
        beats_voz = a_voz["beats"] / ratio      # la grilla se estira con la voz
        desfase, alin = calcular_desfase(beats_voz, a_base["beats"],
                                         t_entrada / ratio, args.desfase)
        largo = base.shape[1]
        if args.duracion:
            largo = min(largo, int(args.duracion * SR))
            base = base[:, :largo]
        voz = desplazar(voz, desfase, largo)
        mix = mezclar(voz, base, args.gain_voz)

        nombre = (f"{clave(args.voz).rsplit('-', 1)[0][:40]}--sobre--"
                  f"{clave(args.base).rsplit('-', 1)[0][:40]}")
        destino = SALIDA / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{nombre}"
        salida = exportar(mix, destino)
        salida.with_suffix(".json").write_text(json.dumps({
            "voz": args.voz, "base": args.base,
            "analisis": {k: {**{x: v for x, v in a.items() if x != "beats"}}
                         for k, a in (("voz", a_voz), ("base", a_base))},
            "tempo_ratio": round(ratio, 4), "semitonos": semi,
            "desfase_s": round(desfase, 3), "alineacion": alin,
            "parametros": {"desfase_beats": args.desfase,
                           "gain_voz_db": args.gain_voz,
                           "sin_transponer": args.sin_transponer},
            "motor_estiramiento": "rubberband" if _rubberband() else "librosa",
            "avisos": avisos,
            "generado": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2))
        print(f"\nguardado: {salida.resolve()}")
        print(f"la voz entra en el segundo {alin['entrada_en_base_s']} de la "
              f"base. Si cae a destiempo: --desfase 4 (o -4) y repetí: los "
              "stems quedan en cache, la segunda vuelta tarda segundos.")
        return 0

    except (RuntimeError, ValueError, FileNotFoundError) as e:
        logger.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
