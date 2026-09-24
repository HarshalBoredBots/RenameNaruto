FROM python:3.10-slim

# System binaries required:
#   ffmpeg  — metadata injection, sample clips, screenshot grids, stream copy
#   ffprobe — media duration detection, MediaInfo (/mi command)
#   Both come from the single 'ffmpeg' apt package.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Koyeb health-check port
EXPOSE 8000

CMD ["python3", "bot.py"]
