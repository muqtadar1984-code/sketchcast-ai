# The books runner image (R1). One AWS Fargate task per book run; the app starts it with
#   command = ["python", "-m", "publishing.runner", "--run-id", "<id>"]
# Build context: the repository root.
#   docker build -f publishing/runner/Dockerfile -t sketchcast-books-runner .
#
# Railway never sees this file: it builds the worker with Nixpacks from the repo root.
# S3 (drafting + rendering on Linux) adds: COPY spike/ (book_assets imports spike.scene_engine inside its
# image functions), Chromium + the chosen open font, and pgserver for the local catalogue mirror.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
COPY publishing/runner/requirements.txt runner-requirements.txt
RUN pip install -r requirements.txt -r runner-requirements.txt

# only what a book run imports: the engine, the shared model clients, the prompts and configs they read
COPY publishing/ publishing/
COPY shared/ shared/

RUN useradd --create-home runner && chown -R runner /app
USER runner

ENTRYPOINT ["python", "-m", "publishing.runner"]
