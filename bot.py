import discord
import asyncio
import aiohttp
import os
import io
import wave
import time
import requests
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

# Configuracion
SILENCE_TIMEOUT = 30        # segundos de silencio antes de terminar
MAX_SEGMENT_DURATION = 540  # 9 minutos por segmento (margen antes de 25MB)
CHUNK_SIZE = 3840           # bytes por chunk de audio (20ms a 48kHz stereo)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Estado global de grabaciones activas
active_recordings = {}


class AudioSink(discord.AudioSink):
    """Recibe y acumula el audio de todos los speakers."""

    def __init__(self):
        self.audio_data = {}       # {user_id: [chunks]}
        self.last_audio_time = time.time()
        self.combined_chunks = []  # audio mezclado

    def write(self, data, user):
        self.last_audio_time = time.time()
        if user not in self.audio_data:
            self.audio_data[user] = []
        self.audio_data[user].append(data.data)
        self.combined_chunks.append(data.data)

    def get_audio_bytes(self):
        """Combina todo el audio en un archivo WAV en memoria."""
        if not self.combined_chunks:
            return None

        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wf:
            wf.setnchannels(2)       # stereo
            wf.setsampwidth(2)       # 16-bit
            wf.setframerate(48000)   # 48kHz
            for chunk in self.combined_chunks:
                wf.writeframes(chunk)

        buffer.seek(0)
        return buffer

    def cleanup(self):
        pass


class RecordingSession:
    """Maneja la sesión de grabación de un canal."""

    def __init__(self, voice_client, channel, text_channel):
        self.voice_client = voice_client
        self.channel = channel
        self.text_channel = text_channel
        self.sink = None
        self.segments = []
        self.is_recording = False
        self.start_time = time.time()
        self.segment_start_time = time.time()
        self.stopped_manually = False

    async def start(self):
        self.is_recording = True
        self.sink = AudioSink()
        self.voice_client.start_recording(
            self.sink,
            self.on_recording_finished,
            self.text_channel
        )
        await self.text_channel.send("🔴 **Grabación iniciada.** Hablen con normalidad.\nUsá `!stop` para detener manualmente.")
        asyncio.create_task(self.monitor())

    async def monitor(self):
        """Monitorea silencio, duración máxima y si el canal quedó vacío."""
        while self.is_recording:
            await asyncio.sleep(5)

            if not self.is_recording:
                break

            # Verificar si el canal quedó vacío (sin humanos)
            members = [m for m in self.channel.members if not m.bot]
            if len(members) == 0:
                await self.text_channel.send("👋 Canal vacío — deteniendo grabación automáticamente.")
                await self.stop()
                break

            # Verificar silencio prolongado
            if self.sink and (time.time() - self.sink.last_audio_time) > SILENCE_TIMEOUT:
                await self.text_channel.send(f"🔇 {SILENCE_TIMEOUT}s de silencio — deteniendo grabación.")
                await self.stop()
                break

            # Verificar duración máxima del segmento
            if (time.time() - self.segment_start_time) > MAX_SEGMENT_DURATION:
                await self.rotate_segment()

    async def rotate_segment(self):
        """Guarda el segmento actual y empieza uno nuevo."""
        await self.text_channel.send("⏱️ Segmento de 9 minutos completado, iniciando nuevo segmento...")

        # Detener grabacion actual y guardar segmento
        self.voice_client.stop_recording()
        await asyncio.sleep(1)

        audio = self.sink.get_audio_bytes()
        if audio:
            self.segments.append(audio)

        # Iniciar nuevo segmento
        self.sink = AudioSink()
        self.segment_start_time = time.time()
        self.voice_client.start_recording(
            self.sink,
            self.on_recording_finished,
            self.text_channel
        )

    async def stop(self):
        if not self.is_recording:
            return
        self.is_recording = False
        self.voice_client.stop_recording()

    def on_recording_finished(self, sink, text_channel, *args):
        """Callback cuando termina la grabación — envía a n8n."""
        audio = sink.get_audio_bytes()
        if audio:
            self.segments.append(audio)

        asyncio.create_task(self.send_to_n8n())

    async def send_to_n8n(self):
        """Envía cada segmento al webhook de n8n."""
        if not self.segments:
            await self.text_channel.send("⚠️ No se capturó audio.")
            await self.cleanup()
            return

        await self.text_channel.send(f"⚙️ Procesando {len(self.segments)} segmento(s)...")

        async with aiohttp.ClientSession() as session:
            for i, audio_segment in enumerate(self.segments):
                try:
                    form = aiohttp.FormData()
                    form.add_field(
                        'audio',
                        audio_segment,
                        filename=f'segment_{i+1}.wav',
                        content_type='audio/wav'
                    )
                    form.add_field('segment', str(i + 1))
                    form.add_field('total_segments', str(len(self.segments)))
                    form.add_field('channel_id', str(self.channel.id))
                    form.add_field('channel_name', self.channel.name)
                    form.add_field('guild_id', str(self.channel.guild.id))

                    async with session.post(N8N_WEBHOOK_URL, data=form, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status == 200:
                            await self.text_channel.send(f"✅ Segmento {i+1}/{len(self.segments)} enviado a n8n.")
                        else:
                            await self.text_channel.send(f"❌ Error enviando segmento {i+1}: HTTP {resp.status}")

                except Exception as e:
                    await self.text_channel.send(f"❌ Error en segmento {i+1}: {str(e)}")

        await self.cleanup()

    async def cleanup(self):
        guild_id = self.channel.guild.id
        if guild_id in active_recordings:
            del active_recordings[guild_id]
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()


# ── Comandos ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")


@bot.command(name="start")
async def start_recording(ctx):
    """!start — El bot se une al canal de voz y empieza a grabar."""
    if ctx.guild.id in active_recordings:
        await ctx.send("⚠️ Ya hay una grabación activa en este servidor.")
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Tenés que estar en un canal de voz para usar este comando.")
        return

    voice_channel = ctx.author.voice.channel

    try:
        voice_client = await voice_channel.connect()
    except Exception as e:
        await ctx.send(f"❌ No pude conectarme al canal: {str(e)}")
        return

    session = RecordingSession(voice_client, voice_channel, ctx.channel)
    active_recordings[ctx.guild.id] = session
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
    """!status — Muestra si hay una grabación activa."""
    if ctx.guild.id in active_recordings:
        session = active_recordings[ctx.guild.id]
        elapsed = int(time.time() - session.start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        await ctx.send(f"🔴 Grabación activa — {mins}m {secs}s transcurridos en **{session.channel.name}**")
    else:
        await ctx.send("⚪ No hay grabación activa.")


# ── Auto-join cuando todos se van ─────────────────────────────────────────────

@bot.event
async def on_voice_state_update(member, before, after):
    """Detecta cuando el canal queda vacío para detener la grabación."""
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)