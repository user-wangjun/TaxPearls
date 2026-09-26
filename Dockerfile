FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# apt 换国内镜像源：香港直连 deb.debian.org 实测仅 ~39KB/s，会卡死构建
RUN sed -i 's|^URIs: http://deb.debian.org/|URIs: https://mirrors.aliyun.com/|' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home taxpearls \
    && mkdir -p /data && chown taxpearls:taxpearls /data
COPY src ./src
COPY webapp ./webapp
COPY rules ./rules
COPY templates ./templates
COPY logo ./logo
ENV TAXPEARLS_HOST=0.0.0.0 TAXPEARLS_PORT=8000 TAXPEARLS_DB=/data/taxpearls.db TAXPEARLS_PDF_BROWSER=chromium
USER taxpearls
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
# Single process: review drafts and AI jobs live in memory.
CMD ["python", "-m", "webapp"]
