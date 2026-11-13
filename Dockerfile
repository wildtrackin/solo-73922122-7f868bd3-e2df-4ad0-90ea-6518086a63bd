# 测量工作簿重算服务：FastAPI + LibreOffice(headless, UNO)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RECALC_STORAGE_ROOT=/data/tasks

# libreoffice-nogui 提供 soffice 与 python3-uno（系统 python3 可 import uno）
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        libreoffice-nogui \
        libreoffice-calc \
        python3-uno \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt /srv/requirements.txt
# 依赖安装到独立前缀，避免污染/触碰 Debian 系统 python（PEP 668）
RUN pip3 install --no-cache-dir --prefix=/usr/local -r requirements.txt

COPY app /srv/app
COPY samples /samples-src
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/tasks /samples \
    && chown -R appuser:appuser /data /samples /srv

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
