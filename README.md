# Discord Meeting Transcriptor

Bot de Discord que se une a un canal de voz, escucha a cada participante por separado, transcribe la reunión **con el nombre de quien habla**, genera un resumen con IA y publica el resultado en un canal de texto de Discord.

> ⚠️ **Antes de usarlo:** grabar la voz de personas requiere su consentimiento. Ver [Privacidad](#privacidad-y-consentimiento).

---

## Cómo funciona

```
Canal de voz (Discord)
   │  audio separado por participante (SSRC → user.id)
   ▼
bot.py  ──►  segmenta por intervención + timestamp
   │         transcribe cada intervención (OpenAI gpt-4o-transcribe)
   │         filtra alucinaciones (idioma, scripts, muletillas)
   ▼
Webhook de n8n  ──►  resumen con LLM  ──►  .md de la reunión
                                              │
                                              ▼
                                  Canal de Discord (resumen + adjunto)
```

El bot **no mezcla** las pistas de audio: como Discord entrega cada voz con su identificador, la atribución de hablante es exacta y usa el nombre real de cada participante.

---

## Requisitos

- **Python 3.11** (o Docker, recomendado).
- **Un host siempre encendido con UDP saliente sin restricciones.** La voz de Discord viaja por UDP a `*.discord.media`. Plataformas que duermen procesos o proxean la red (por ejemplo, Render free) **no funcionan**.
- **Linux con `libopus0` y `ffmpeg`** (el `Dockerfile` ya los instala).
- **API key de OpenAI** para la transcripción.
- **n8n** (self-hosted o cloud) con el webhook accesible desde el bot.
- **Un LLM para el resumen** en n8n (OpenAI, OpenRouter, Groq, etc.).

---

## 1. Crear la aplicación de Discord

1. Entrá al [Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. En **Bot**:
   - Generá el **Token** y guardalo (es el `DISCORD_TOKEN`).
   - Activá los **Privileged Gateway Intents**:
     - ✅ **Server Members Intent**
     - ✅ **Message Content Intent**
3. En **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Permisos del bot: `View Channels`, `Send Messages`, `Read Message History`, `Connect`
4. Abrí la URL generada y agregá el bot a tu servidor.

> Si el resumen lo publica un bot distinto desde n8n, ese bot también necesita `Send Messages` y `Attach Files` en el canal de resumen.

---

## 2. Instalación

```bash
git clone https://github.com/TU_USUARIO/discord-transcriptor-bot.git
cd discord-transcriptor-bot
cp .env.example .env     # completá las variables (ver tabla abajo)
```

### Opción A: Docker (recomendada)

```bash
docker compose up -d --build
docker compose logs -f
```

El `docker-compose.yml` ya incluye reinicio automático (`restart: unless-stopped`) y un volumen para `failed_payloads/`.

### Opción B: Python directo

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

En Linux instalá además `libopus0` (`sudo apt install libopus0 ffmpeg`).

---

## 3. Variables de entorno

Todas se configuran en `.env`. Las marcadas como **obligatorias** no tienen valor por defecto.

| Variable | Obligatoria | Default | Descripción |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Token del bot de Discord |
| `N8N_WEBHOOK_URL` | ✅ | — | URL del webhook de n8n (`.../webhook/discord-audio`) |
| `OPENAI_API_KEY` | ✅ | — | Key de OpenAI para la transcripción |
| `TRANSCRIBE_MODEL` | | `gpt-4o-transcribe` | Modelo de transcripción |
| `TRANSCRIBE_BASE_URL` | | `https://api.openai.com/v1` | Endpoint compatible con OpenAI |
| `TRANSCRIBE_API_KEY` | | `OPENAI_API_KEY` | Key específica, si usás otro proveedor |
| `TRANSCRIBE_LANGUAGE` | | `es` | Idioma forzado |
| `TRANSCRIBE_PROMPT` | | vacío | Términos y nombres propios para mejorar la precisión |
| `LANG_FILTER` | | `1` | `0` desactiva el filtro de idioma en clips cortos |
| `SHORT_UTTERANCE_MAX_WORDS` | | `2` | Hasta cuántas palabras se considera "clip corto" |
| `SPANISH_EXTRA_ALLOWLIST` | | vacío | Muletillas propias a proteger, separadas por coma |
| `FAILED_PAYLOAD_DIR` | | `failed_payloads` | Dónde guardar reuniones si n8n no responde |
| `PORT` | | `8080` | Puerto del health check HTTP |

**Ejemplo de `.env`:**

```dotenv
DISCORD_TOKEN=TU_DISCORD_TOKEN
N8N_WEBHOOK_URL=https://tu-n8n.com/webhook/discord-audio
OPENAI_API_KEY=TU_OPENAI_API_KEY

TRANSCRIBE_MODEL=gpt-4o-transcribe
TRANSCRIBE_PROMPT=Reunión de equipo de desarrollo. Términos: NombreProducto, Cliente, Proyecto.

SPANISH_EXTRA_ALLOWLIST=epa,bueh
```

> Para usar un proveedor compatible con OpenAI distinto (por ejemplo OpenRouter o Groq), cambiá también `TRANSCRIBE_BASE_URL`, `TRANSCRIBE_MODEL` y `TRANSCRIBE_API_KEY` juntos. Si cambiás solo la key, las llamadas van a fallar.

---

## 4. Configurar n8n

1. Importá el workflow desde `workflow/`:
   - n8n → **Workflows → Import from File**.
2. Configurá las credenciales:
   - **LLM del resumen:** en el nodo del modelo de chat, creá la credencial de tu proveedor.
   - **Discord:** en el nodo de envío, el bot con permiso de escribir en el canal de resumen.
3. Reemplazá los placeholders del workflow (`REPLACE_CHANNEL_ID`, etc.) por tus valores.
4. Activá el workflow y copiá la URL del webhook de producción en `N8N_WEBHOOK_URL`.

---

## 5. Uso

Dentro de un canal de texto del servidor, con el usuario que va a estar en el canal de voz:

| Comando | Qué hace |
|---|---|
| `!start [nombre]` | Se une al canal de voz donde estás y empieza a grabar. El nombre es opcional y se usa como título de la reunión. |
| `!status` | Muestra tiempo grabado, participantes detectados y avance. |
| `!stop` | Detiene la grabación, transcribe lo pendiente y envía el resultado a n8n. |

**Además:**
- Si el canal de voz queda vacío, la grabación se detiene sola.
- Si pasan 30 segundos de silencio, también se detiene.
- Cada 5 minutos el bot transcribe lo acumulado y libera memoria, así las reuniones largas no consumen RAM sin límite.

La transcripción **no es en vivo**: se procesa en los flushes periódicos y al detener la grabación.

---

## Privacidad y consentimiento

- **Avisá a los participantes antes de grabar.** El bot anuncia en el canal de texto que la grabación comenzó, pero el consentimiento tiene que ser explícito de cada persona, según la ley aplicable en tu jurisdicción.
- **El audio no se guarda en disco.** Se procesa en memoria y se envía a OpenAI para transcribirlo.
- **Las transcripciones sí pueden quedar en disco** si n8n no responde: se guardan como `.json` en `failed_payloads/`. Tratá esa carpeta como información sensible y no la subas al repo (ya está en `.gitignore`).
- **Revisá la política de datos de tu proveedor de transcripción y LLM.**

---

## Limitaciones conocidas

- **Hosts con red restringida:** si el firewall bloquea UDP saliente, el bot se conecta pero no recibe audio.
- **Redes inestables:** la pérdida de paquetes UDP genera huecos en la transcripción. Conviene conexión por cable.
- **Alucinaciones del modelo:** en clips muy cortos o con silencio, los modelos de transcripción pueden inventar palabras. El bot las filtra en lo posible, pero no todas.
- **Dependencias de terceros:** `discord-ext-voice-recv` está en una versión preliminar (`0.5.2a179`), y `davey` es necesario para el cifrado E2EE (DAVE) de Discord. Si Discord cambia el protocolo de voz, puede hacer falta actualizarlas.
- **Costos:** la transcripción y el LLM se cobran por uso.

---

## Troubleshooting

| Síntoma | Causa probable | Solución |
|---|---|---|
| `libopus NO está disponible` | Falta la librería de Opus | `sudo apt install libopus0` (o usar Docker) |
| El bot se conecta pero no recibe audio | UDP bloqueado o host que duerme | Verificar egress UDP y usar un host persistente |
| `n8n HTTP 503` / no llega a n8n | n8n caído o reiniciándose | El bot reintenta y, si falla, guarda la reunión en `failed_payloads/` |
| Palabras en otros idiomas | Alucinación en clips cortos | Ajustar `TRANSCRIBE_PROMPT` o `SPANISH_EXTRA_ALLOWLIST`; `LANG_FILTER` ya está activo |
| `Falta la API key de transcripción` | `.env` incompleto o mal cargado | Revisar `OPENAI_API_KEY` y reiniciar el bot |

---

## Estructura del repo

```
.
├── bot.py                  # bot de captura y transcripción
├── requirements.txt        # dependencias con versiones fijadas
├── Dockerfile              # Python 3.11 + libopus0 + ffmpeg
├── docker-compose.yml      # despliegue con reinicio automático
├── .env.example            # plantilla de configuración (sin secretos)
├── .gitignore              # excluye .env y failed_payloads/
├── workflow/               # workflow de n8n (sin credenciales)
└── README.md
```

---

## Licencia

TBD
