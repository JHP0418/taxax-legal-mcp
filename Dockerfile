FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY src/taxax/legal ./src/taxax/legal
COPY src/taxax/mcp ./src/taxax/mcp
RUN python -m pip wheel --wheel-dir /wheels -r requirements.lock && \
    python -m pip wheel --wheel-dir /wheels --no-deps .

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TAXAX_LEGAL_DATA_DIR=/data
RUN groupadd --system taxax && \
    useradd --system --gid taxax --home-dir /nonexistent --shell /usr/sbin/nologin taxax && \
    mkdir /data && chown taxax:taxax /data
COPY --from=builder /wheels /wheels
# 버전을 고정하면 릴리스를 올릴 때마다 여기도 같이 고쳐야 하고, 잊으면 빌드가
# 깨진다(실제로 0.2.0에 멈춰 있어 0.2.8 빌드가 실패했다). /wheels에는 방금 만든
# 우리 wheel 하나뿐이라 이름만으로 충분하다.
RUN python -m pip install --no-index --find-links=/wheels taxax-legal-mcp && rm -rf /wheels
USER taxax
VOLUME ["/data"]
EXPOSE 8765
ENTRYPOINT ["taxax-legal-mcp"]
CMD ["--transport", "stdio"]
