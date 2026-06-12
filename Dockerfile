FROM python:3.11-slim

# libopus0: REQUERIDO para decodificar la voz de Discord (discord-ext-voice-recv)
# ffmpeg: util por si en el futuro se quiere reprocesar/comprimir el audio
RUN apt-get update && \
    apt-get install -y --no-install-recommends libopus0 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
