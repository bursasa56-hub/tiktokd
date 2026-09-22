FROM mwader/static-ffmpeg:9.0.2 AS ffmpeg

FROM python:3.12-slim

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data

CMD ["python", "run.py"]
