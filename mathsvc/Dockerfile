# Isolated build for the SymPy math microservice — deliberately does NOT inherit
# the worker's heavy nixpacks build (ffmpeg, pymupdf, …). Build context is the
# REPO ROOT (so `COPY mathsvc` works); on Railway set the service's Dockerfile
# path to `mathsvc/Dockerfile`. Absolute imports (`from mathsvc.ops`) resolve
# because the package sits at /app/mathsvc.
FROM python:3.12-slim

WORKDIR /app

# Install just this service's deps first so layer caching survives code edits.
COPY mathsvc/requirements.txt ./mathsvc/requirements.txt
RUN pip install --no-cache-dir -r mathsvc/requirements.txt

COPY mathsvc ./mathsvc

# Railway injects $PORT; default for local `docker run`.
ENV PORT=8090
CMD ["sh", "-c", "uvicorn mathsvc.app:app --host 0.0.0.0 --port ${PORT}"]
