FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl is used by the HEALTHCHECK below; everything else ships in wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash app
USER app
WORKDIR /home/app/app
ENV PATH="/home/app/.local/bin:${PATH}"

# Install deps first so code edits don't invalidate the pip layer.
COPY --chown=app:app requirements.txt ./
RUN pip install --user -r requirements.txt

COPY --chown=app:app . .

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py", \
     "--server.headless=true", \
     "--server.address=0.0.0.0", \
     "--server.port=8501"]
