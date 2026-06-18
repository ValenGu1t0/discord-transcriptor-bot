import discord
import asyncio
import aiohttp
import os
import io
import wave
import time
import queue
import logging
from datetime import datetime, timezone
from discord.ext import commands
from discord.ext.voice_recv import VoiceRecvClient, BasicSink

# davey: bindings del protocolo DAVE (E2EE de voz de Discord).
# Viene incluido con discord.py[voice] >= 2.7.
try:
    import davey
except ImportError:
    davey = None

# audioop: stdlib en Python 3.11 (lo usamos para downmix estéreo->mono antes
# de subir a Whisper). OJO: audioop se elimina en Python 3.13; si algún día
# actualizan, instalar 'audioop-lts' o reemplazar por numpy.
try:
    import audioop
except ImportError:
    audioop = None

from dotenv import load_dotenv
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("transcriptor")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

# Transcripción: configurable. Por defecto OpenAI gpt-4o-transcribe.
# Para volver a Groq: TRANSCRIBE_BASE_URL=https://api.groq.com/openai/v1
#                     TRANSCRIBE_MODEL=whisper-large-v3-turbo
TRANSCRIBE_BASE_URL = os.getenv("TRANSCRIBE_BASE_URL", "https://api.openai.com/v1")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
TRANSCRIBE_API_KEY = os.getenv("TRANSCRIBE_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "es")
# Prompt opcional para mejorar nombres propios / jerga del dominio (ver notas).
TRANSCRIBE_PROMPT = os.getenv("TRANSCRIBE_PROMPT", "")
TRANSCRIBE_URL = f"{TRANSCRIBE_BASE_URL.rstrip('/')}/audio/transcriptions"

# ── Configuración ─────────────────────────────────────────────────────────────
SILENCE_TIMEOUT = 30          # seg de silencio antes de terminar (tras haber recibido audio)
NO_AUDIO_TIMEOUT = 60         # seg de gracia si NUNCA llegó audio (problema técnico)

# Segmentación por hablante / utterances
# Gap más alto => intervenciones más largas y con contexto: mejor transcripción,
# menos alucinaciones y menos llamadas a la API.
UTTERANCE_GAP = 1.5           # seg de silencio de un mismo hablante => cierra su intervención
MAX_UTTERANCE_DURATION = 30.0 # corta intervenciones muy largas
MIN_UTTERANCE_DURATION = 0.6  # descarta clips muy cortos (ruido, "ajá") que disparan alucinaciones
FLUSH_INTERVAL = 300          # cada 5 min transcribe lo acumulado y libera el audio

# Audio crudo de Discord
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2              # 16-bit
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

# Transcripción
WHISPER_CONCURRENCY = 4       # llamadas simultáneas a la API (cuidando rate limits)
TRANSCRIBE_MAX_RETRIES = 3
TRANSCRIBE_TIMEOUT = 90

# Frases que los modelos STT suelen alucinar sobre silencio/ruido. Si el output
# ES EXACTAMENTE una de estas, se descarta. Ajustá la lista según tu caso.
HALLUCINATION_BLOCKLIST = {
    "gracias.", "gracias", "muchas gracias.", "muchas gracias",
    "gracias por ver el video", "gracias por ver el video.",
    "¡gracias por ver el video!", "subtítulos realizados por la comunidad de amara.org",
    "subtítulos por la comunidad de amara.org", "subtítulos en español de amara.org",
    "¡suscríbete!", "¡suscríbete al canal!",
}

# ──────────────────────────────────────────────────────────────────────────────
# CONTRATO con n8n (lo que manda este bot al webhook, como JSON):
#
# {
#   "titulo":        str,            # nombre de !start, o "<canal> — <fecha>"
#   "canal":         str,
#   "fecha":         str,            # ISO 8601 UTC
#   "guild_id":      str,
#   "channel_id":    str,
#   "duracion_seg":  int,
#   "participantes": [str, ...],     # nombres de Discord que hablaron
#   "dialogo": [ {"t": "HH:MM:SS", "hablante": str, "texto": str}, ... ],  # ordenado por tiempo
#   "texto_completo": str            # el diálogo ya formateado como texto plano
# }
#
# n8n lo recibe en $json.body. (Fase 2: armar el .md, resumir con Llama y
# guardar en Airtable.)
# ──────────────────────────────────────────────────────────────────────────────


# ── Carga de Opus ─────────────────────────────────────────────────────────────
# discord-ext-voice-recv necesita libopus para decodificar los paquetes de voz.
# Sin esto, el bot se conecta pero NUNCA recibe PCM (falla silenciosa).
def ensure_opus():
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus._load_default()
        if discord.opus.is_loaded():
            return True
    except Exception:
        pass
    for lib in ("opus", "libopus.so.0", "libopus.0.dylib", "libopus-0.dll", "opus.dll"):
        try:
            discord.opus.load_opus(lib)
            return True
        except Exception:
            continue
    return False


OPUS_OK = ensure_opus()
log.info("Opus cargado: %s | davey (DAVE/E2EE) disponible: %s", OPUS_OK, davey is not None)
if davey is None:
    log.warning("⚠️ El paquete 'davey' no está instalado. Si Discord activa E2EE en el canal, "
                "el audio llegará cifrado y no se podrá decodificar. Instalá: pip install davey")
if not OPUS_OK:
    log.error("⚠️ libopus NO está disponible. La captura de voz NO va a funcionar. "
              "Instalá libopus0 en el contenedor (ver Dockerfile).")
if not TRANSCRIBE_API_KEY:
    log.warning("⚠️ No hay API key de transcripción (TRANSCRIBE_API_KEY / OPENAI_API_KEY). "
                "La transcripción va a fallar. Agregala como variable de entorno.")
log.info("Transcripción: modelo=%s endpoint=%s", TRANSCRIBE_MODEL, TRANSCRIBE_URL)


# ── Utilidades de audio ───────────────────────────────────────────────────────
def pcm_to_wav_mono(pcm: bytes) -> io.BytesIO:
    """Convierte PCM estéreo 48k a un WAV mono (más liviano y mejor para Whisper)."""
    data = pcm
    channels = CHANNELS
    if audioop is not None:
        try:
            data = audioop.tomono(pcm, SAMPLE_WIDTH, 0.5, 0.5)
            channels = 1
        except Exception:
            data, channels = pcm, CHANNELS

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(data)
    buf.seek(0)
    return buf


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def looks_like_hallucination(text: str) -> bool:
    """Detecta salidas típicas de alucinación de los modelos STT sobre silencio/ruido."""
    t = text.strip().lower()
    if t in HALLUCINATION_BLOCKLIST or t.strip(" .!¡¿?") in HALLUCINATION_BLOCKLIST:
        return True
    words = t.strip(" .!¡¿?,").split()
    # Repetición degenerada (ej. "éxito, éxito, éxito, éxito...")
    if len(words) >= 4 and len(set(words)) <= 2:
        return True
    return False


# ── Stream por hablante ───────────────────────────────────────────────────────
class UserStream:
    """
    Acumula el audio de UN hablante y lo parte en 'intervenciones' (utterances)
    según los silencios. Solo se toca desde el event loop (el hilo de voz nunca
    accede acá: se comunica vía cola), así que no necesita locks.
    """

    def __init__(self, display_name: str):
        self.display_name = display_name
        self._cur_chunks = []      # PCM de la intervención en curso
        self._cur_start = None     # offset (seg desde inicio) en que arrancó
        self._last_offset = None   # offset del último paquete recibido
        self.finished = []         # intervenciones cerradas, pendientes de transcribir

    def _close(self):
        if self._cur_chunks and self._cur_start is not None:
            self.finished.append({
                "start": self._cur_start,
                "speaker": self.display_name,
                "pcm": b"".join(self._cur_chunks),
            })
        self._cur_chunks = []
        self._cur_start = None

    def add_packet(self, offset: float, pcm: bytes):
        # ¿Pasó suficiente silencio como para cerrar la intervención anterior?
        if self._last_offset is not None and (offset - self._last_offset) > UTTERANCE_GAP:
            self._close()
        # ¿La intervención en curso ya es demasiado larga? La cortamos.
        elif self._cur_start is not None and (offset - self._cur_start) > MAX_UTTERANCE_DURATION:
            self._close()

        if self._cur_start is None:
            self._cur_start = offset
        self._cur_chunks.append(pcm)
        self._last_offset = offset

    def take_finished(self, close_current: bool = False):
        """Devuelve las intervenciones cerradas y limpia el buffer."""
        if close_current:
            self._close()
        items = self.finished
        self.finished = []
        return items


# ── Sesión de grabación ───────────────────────────────────────────────────────
class RecordingSession:
    """Maneja la grabación de un canal de voz."""

    def __init__(self, voice_client, channel, text_channel, title=None):
        self.voice_client = voice_client
        self.channel = channel
        self.text_channel = text_channel
        self.title = title

        self.is_recording = False
        self.start_time = time.time()
        self.last_flush_time = time.time()
        self.last_audio_time = time.time()
        self.received_any_audio = False
        self.stopped_manually = False

        self.packet_count = 0
        self.dropped_packets = 0

        self.decoders = {}                 # user_id -> discord.opus.Decoder (estado por hablante)
        self.audio_queue = queue.Queue()   # (user_id, name, offset, pcm) -- hilo de voz -> event loop
        self.user_streams = {}             # user_id -> UserStream (solo desde el event loop)
        self.participants = set()
        self.dialogue = []                 # [{start, speaker, text}] ya transcrito

        self.consumer_task = None
        self.monitor_task = None

    # ── Captura (corre en el HILO DE VOZ) ─────────────────────────────────────
    async def start(self):
        self.is_recording = True

        # WORKAROUND DAVE (E2EE): discord.py 2.7.x negocia DAVE, pero
        # discord-ext-voice-recv (<= 0.5.2a179) intenta decodificar sin quitar
        # primero la capa E2EE -> "OpusError: corrupted stream". Solución:
        # pedir Opus crudo (decode=False), descifrar DAVE a mano y decodificar
        # nosotros con un decoder por usuario.
        def audio_callback(user, data):
            # OJO: esto corre en el hilo de voz, NO en el event loop.
            try:
                if user is None:
                    return  # paquete sin SSRC mapeado todavía

                payload = data.opus
                if not payload:
                    return
                opus_payload = bytes(payload)

                # 1) Quitar la capa E2EE (DAVE) si está activa
                dave = getattr(self.voice_client._connection, "dave_session", None)
                if davey is not None and dave is not None and dave.ready:
                    try:
                        decrypted = dave.decrypt(user.id, davey.MediaType.audio, opus_payload)
                        if decrypted:
                            opus_payload = bytes(decrypted)
                    except Exception:
                        pass  # paquete puede no estar cifrado (transición)

                # 2) Decodificar Opus -> PCM (decoder con estado, uno por usuario)
                dec = self.decoders.get(user.id)
                if dec is None:
                    dec = self.decoders[user.id] = discord.opus.Decoder()
                pcm = dec.decode(opus_payload, fec=False)
                if not pcm:
                    return

                # 3) Encolar con timestamp y nombre. NO procesamos acá: el hilo
                #    de voz tiene que ser rápido. La segmentación va en el loop.
                offset = time.time() - self.start_time
                try:
                    name = user.display_name
                except Exception:
                    name = f"Usuario {user.id}"
                self.audio_queue.put_nowait((user.id, name, offset, pcm))

                self.packet_count += 1
                self.last_audio_time = time.time()
                if self.packet_count == 1:
                    self.received_any_audio = True
                    log.info("🎧 Primer paquete decodificado (user=%s, %d bytes PCM, dave=%s)",
                             name, len(pcm), bool(dave and dave.ready))
                elif self.packet_count % 500 == 0:
                    log.info("🎧 %d paquetes acumulados", self.packet_count)

            except discord.opus.OpusError as e:
                self.dropped_packets += 1
                if self.dropped_packets in (1, 10, 100) or self.dropped_packets % 1000 == 0:
                    log.warning("Paquete opus descartado (#%d): %r", self.dropped_packets, e)
            except Exception as e:
                log.error("Error en audio_callback: %r", e)

        sink = BasicSink(audio_callback, decode=False)
        self.voice_client.listen(sink)
        log.info("Escuchando en canal '%s' (opus_loaded=%s, davey=%s)",
                 self.channel.name, discord.opus.is_loaded(), davey is not None)

        await self.text_channel.send(
            "🔴 **Grabación iniciada.** Hablen con normalidad.\n"
            "Voy a identificar a cada hablante por su nombre de Discord.\n"
            "Usá `!stop` para detener."
        )

        self.consumer_task = asyncio.create_task(self.process_queue())
        self.monitor_task = asyncio.create_task(self.monitor())

    # ── Consumidor de la cola (event loop, sin locks) ─────────────────────────
    async def process_queue(self):
        while self.is_recording or not self.audio_queue.empty():
            drained = 0
            while True:
                try:
                    user_id, name, offset, pcm = self.audio_queue.get_nowait()
                except queue.Empty:
                    break
                st = self.user_streams.get(user_id)
                if st is None:
                    st = self.user_streams[user_id] = UserStream(name)
                else:
                    st.display_name = name  # refrescar por si se resolvió después
                self.participants.add(name)
                st.add_packet(offset, pcm)
                drained += 1
                if drained >= 5000:
                    break  # ceder el control de vez en cuando
            await asyncio.sleep(0.3)

    # ── Transcripción ─────────────────────────────────────────────────────────
    async def _transcribe(self, session, sem, utt):
        pcm = utt["pcm"]
        duration = len(pcm) / BYTES_PER_SEC
        if duration < MIN_UTTERANCE_DURATION:
            return None

        wav = pcm_to_wav_mono(pcm)
        async with sem:
            for attempt in range(TRANSCRIBE_MAX_RETRIES):
                try:
                    wav.seek(0)
                    form = aiohttp.FormData()
                    form.add_field("file", wav, filename="utt.wav", content_type="audio/wav")
                    form.add_field("model", TRANSCRIBE_MODEL)
                    if TRANSCRIBE_LANGUAGE:
                        form.add_field("language", TRANSCRIBE_LANGUAGE)
                    form.add_field("response_format", "json")
                    if TRANSCRIBE_PROMPT:
                        form.add_field("prompt", TRANSCRIBE_PROMPT)
                    async with session.post(
                        TRANSCRIBE_URL,
                        data=form,
                        headers={"Authorization": f"Bearer {TRANSCRIBE_API_KEY}"},
                        timeout=aiohttp.ClientTimeout(total=TRANSCRIBE_TIMEOUT),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            text = (data.get("text") or "").strip()
                            if not text or looks_like_hallucination(text):
                                return None
                            return {"start": utt["start"], "speaker": utt["speaker"], "text": text}
                        body = await resp.text()
                        log.warning("Transcribe HTTP %s (intento %d): %s", resp.status, attempt + 1, body[:200])
                        # Errores no recuperables (auth, request inválida): no reintentar.
                        if resp.status not in (429, 500, 502, 503, 504):
                            break
                except Exception as e:
                    log.warning("Transcribe error (intento %d): %r", attempt + 1, e)
                # Backoff exponencial (clave para 429 y picos de red).
                await asyncio.sleep(min(1.5 * (2 ** attempt), 8))
        # Falló tras reintentos: marca para no perder el hueco.
        return {"start": utt["start"], "speaker": utt["speaker"], "text": "[inaudible]"}

    async def flush(self, final: bool = False) -> int:
        """Transcribe todo lo acumulado y lo agrega al diálogo. Libera el audio."""
        batch = []
        for st in self.user_streams.values():
            batch.extend(st.take_finished(close_current=final))
        if not batch:
            return 0

        sem = asyncio.Semaphore(WHISPER_CONCURRENCY)
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*[self._transcribe(session, sem, u) for u in batch])

        added = 0
        for r in results:
            if r:
                self.dialogue.append(r)
                added += 1
        log.info("flush(final=%s): %d intervenciones transcriptas", final, added)
        return added

    # ── Monitoreo ─────────────────────────────────────────────────────────────
    async def monitor(self):
        while self.is_recording:
            await asyncio.sleep(5)
            if not self.is_recording:
                break

            # Canal vacío -> cortar
            members = [m for m in self.channel.members if not m.bot]
            if len(members) == 0:
                await self.text_channel.send("👋 Canal vacío — deteniendo grabación automáticamente.")
                await self.stop()
                break

            elapsed_since_audio = time.time() - self.last_audio_time

            if not self.received_any_audio:
                if elapsed_since_audio > NO_AUDIO_TIMEOUT:
                    await self.text_channel.send(
                        f"⚠️ Pasaron {NO_AUDIO_TIMEOUT}s y **no se recibió ningún paquete de audio**. "
                        "Suele indicar un problema de captura (¿libopus instalado?). Deteniendo."
                    )
                    log.error("Sin audio en %ds. opus_loaded=%s", NO_AUDIO_TIMEOUT, discord.opus.is_loaded())
                    await self.stop()
                    break
            else:
                if elapsed_since_audio > SILENCE_TIMEOUT:
                    await self.text_channel.send(f"🔇 {SILENCE_TIMEOUT}s de silencio — deteniendo grabación.")
                    await self.stop()
                    break

            # Transcripción incremental (mantiene la memoria acotada en reuniones largas)
            if (time.time() - self.last_flush_time) > FLUSH_INTERVAL:
                self.last_flush_time = time.time()
                await self.flush(final=False)

    # ── Stop / envío ──────────────────────────────────────────────────────────
    async def stop(self):
        if not self.is_recording:
            return
        self.is_recording = False

        # Evitar que monitor dispare un flush en paralelo (salvo que sea quien llamó).
        if self.monitor_task and self.monitor_task is not asyncio.current_task():
            self.monitor_task.cancel()

        try:
            self.voice_client.stop_listening()
        except Exception as e:
            log.warning("Error al detener listener: %r", e)

        # Drenar lo que quede en la cola antes del flush final.
        if self.consumer_task:
            try:
                await asyncio.wait_for(self.consumer_task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        await self.text_channel.send("⚙️ Transcribiendo y armando el documento...")
        await self.flush(final=True)

        log.info("Grabación detenida. Paquetes: %d, intervenciones: %d, participantes: %d",
                 self.packet_count, len(self.dialogue), len(self.participants))
        await self.send_to_n8n()

    def build_payload(self) -> dict:
        self.dialogue.sort(key=lambda d: d["start"])
        dialogo = [
            {"t": fmt_ts(d["start"]), "hablante": d["speaker"], "texto": d["text"]}
            for d in self.dialogue
        ]
        texto_completo = "\n".join(f"[{x['t']}] {x['hablante']}: {x['texto']}" for x in dialogo)
        title = self.title or f"{self.channel.name} — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        return {
            "titulo": title,
            "canal": self.channel.name,
            "fecha": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(self.channel.guild.id),
            "channel_id": str(self.channel.id),
            "duracion_seg": int(time.time() - self.start_time),
            "participantes": sorted(self.participants),
            "dialogo": dialogo,
            "texto_completo": texto_completo,
        }

    async def send_to_n8n(self):
        if not self.dialogue:
            await self.text_channel.send("⚠️ No se capturó diálogo transcribible.")
            await self.cleanup()
            return

        payload = self.build_payload()

        # Log para poder verificar la diarización incluso antes de tener n8n listo.
        preview = "\n".join(f"[{x['t']}] {x['hablante']}: {x['texto']}" for x in payload["dialogo"][:8])
        log.info("Payload a n8n (%d intervenciones, %d participantes):\n%s",
                 len(payload["dialogo"]), len(payload["participantes"]), preview)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    N8N_WEBHOOK_URL, json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 200:
                        await self.text_channel.send(
                            f"✅ Enviado a n8n — **{len(payload['dialogo'])} intervenciones** de "
                            f"**{', '.join(payload['participantes'])}**."
                        )
                    else:
                        body = await resp.text()
                        await self.text_channel.send(f"❌ n8n respondió HTTP {resp.status}.")
                        log.warning("n8n HTTP %s: %s", resp.status, body[:300])
        except Exception as e:
            await self.text_channel.send(f"❌ Error enviando a n8n: {e}")
            log.error("Error enviando a n8n: %r", e)

        await self.cleanup()

    async def cleanup(self):
        guild_id = self.channel.guild.id
        if guild_id in active_recordings:
            del active_recordings[guild_id]
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()


# Estado global de grabaciones activas
active_recordings = {}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Comandos ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info("✅ Bot conectado como %s | opus_loaded=%s", bot.user, discord.opus.is_loaded())


@bot.command(name="start")
async def start_recording(ctx, *, nombre: str = None):
    """!start [nombre] — Se une al canal de voz y empieza a grabar. 'nombre' es opcional (título de la reunión)."""
    if ctx.guild.id in active_recordings:
        await ctx.send("⚠️ Ya hay una grabación activa en este servidor.")
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Tenés que estar en un canal de voz para usar este comando.")
        return

    if not discord.opus.is_loaded():
        await ctx.send("❌ **libopus no está cargado** — no puedo decodificar audio.")
        return

    if not TRANSCRIBE_API_KEY:
        await ctx.send("❌ Falta la **API key de transcripción** en el entorno — no puedo transcribir.")
        return

    voice_channel = ctx.author.voice.channel
    try:
        voice_client = await voice_channel.connect(cls=VoiceRecvClient)
    except Exception as e:
        await ctx.send(f"❌ No pude conectarme al canal: {e}")
        return

    session = RecordingSession(voice_client, voice_channel, ctx.channel, title=nombre)
    active_recordings[ctx.guild.id] = session
    if nombre:
        await ctx.send(f"📝 Reunión: **{nombre}**")
    await session.start()


@bot.command(name="stop")
async def stop_recording(ctx):
    """!stop — Detiene la grabación manualmente."""
    if ctx.guild.id not in active_recordings:
        await ctx.send("⚠️ No hay ninguna grabación activa.")
        return
    await ctx.send("⏹️ Deteniendo grabación...")
    session = active_recordings[ctx.guild.id]
    session.stopped_manually = True
    await session.stop()


@bot.command(name="status")
async def status(ctx):
    """!status — Muestra el estado de la grabación."""
    if ctx.guild.id in active_recordings:
        session = active_recordings[ctx.guild.id]
        elapsed = int(time.time() - session.start_time)
        people = ", ".join(sorted(session.participants)) or "—"
        await ctx.send(
            f"🔴 Grabando — {elapsed // 60}m {elapsed % 60}s en **{session.channel.name}**\n"
            f"👥 Hablantes detectados: {people}\n"
            f"🗣️ Intervenciones transcriptas: {len(session.dialogue)} | paquetes: {session.packet_count}"
        )
    else:
        await ctx.send("⚪ No hay grabación activa.")


# ── Auto-stop cuando todos se van ─────────────────────────────────────────────
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    guild = member.guild
    if guild.id not in active_recordings:
        return
    session = active_recordings[guild.id]
    members_in_channel = [m for m in session.channel.members if not m.bot]
    if len(members_in_channel) == 0 and session.is_recording:
        await session.text_channel.send("👋 Todos salieron del canal — deteniendo grabación.")
        await session.stop()


# ── HTTP mini server (health check para Render) ───────────────────────────────
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), HealthCheck).serve_forever()


threading.Thread(target=run_health_server, daemon=True).start()


@bot.event
async def on_message(message):
    if not message.author.bot:
        log.info("Mensaje recibido: %s de %s", message.content, message.author)
    await bot.process_commands(message)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)