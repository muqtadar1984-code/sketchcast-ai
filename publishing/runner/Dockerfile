# The books runner image (R1). One AWS Fargate task per book run; the app starts it with
#   command = ["python", "-m", "publishing.runner", "--run-id", "<id>"]
# Build context: the repository root.
#   docker build -f publishing/runner/Dockerfile -t sketchcast-books-runner .
#
# Railway never sees this file: it builds the worker with Nixpacks from the repo root.
# Drafting on the server (S3, next) still adds: COPY spike/ (book_assets imports spike.scene_engine inside its
# image functions) and pgserver for the local catalogue mirror.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Rendering on Linux (S3): Chromium prints the pages (publishing.manuscript.render.browser), fontconfig finds the
# fonts. Noto covers the theme's body stack ('Segoe UI', 'Noto Sans', …) and every script the engine prints; DejaVu
# is the last resort. Sitka (the Biology headings) is Windows-only: the founder picks its open replacement (D3).
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fontconfig fonts-noto-core fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# The two candidate heading fonts (SIL Open Font Licence), pinned to a google/fonts commit so a rebuild never changes
# the book's look.
ADD https://raw.githubusercontent.com/google/fonts/08dc85da6bca7ae308a6f1d38d0b137465646071/ofl/sourceserif4/SourceSerif4%5Bopsz%2Cwght%5D.ttf /usr/share/fonts/truetype/books/SourceSerif4.ttf
ADD https://raw.githubusercontent.com/google/fonts/08dc85da6bca7ae308a6f1d38d0b137465646071/ofl/sourceserif4/SourceSerif4-Italic%5Bopsz%2Cwght%5D.ttf /usr/share/fonts/truetype/books/SourceSerif4-Italic.ttf
ADD https://raw.githubusercontent.com/google/fonts/4e5f06dbb274a27ebe71ed54ea706b3ee40eabd9/ofl/literata/Literata%5Bopsz%2Cwght%5D.ttf /usr/share/fonts/truetype/books/Literata.ttf
ADD https://raw.githubusercontent.com/google/fonts/4e5f06dbb274a27ebe71ed54ea706b3ee40eabd9/ofl/literata/Literata-Italic%5Bopsz%2Cwght%5D.ttf /usr/share/fonts/truetype/books/Literata-Italic.ttf
RUN chmod 644 /usr/share/fonts/truetype/books/*.ttf && fc-cache -f \
 && fc-list | grep -qi "Source Serif 4" && fc-list | grep -qi "Literata"

COPY requirements.txt .
COPY publishing/runner/requirements.txt runner-requirements.txt
RUN pip install -r requirements.txt -r runner-requirements.txt

# only what a book run imports: the engine, the shared model clients, the prompts and configs they read
COPY publishing/ publishing/
COPY shared/ shared/

ENV PUBLISH_BROWSER=/usr/bin/chromium
RUN useradd --create-home runner && chown -R runner /app
USER runner

ENTRYPOINT ["python", "-m", "publishing.runner"]
