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
RUN python -m pip install --no-index --find-links=/wheels taxax-legal-mcp==0.2.0 && rm -rf /wheels
USER taxax
VOLUME ["/data"]
EXPOSE 8765
ENTRYPOINT ["taxax-legal-mcp"]
CMD ["--transport", "stdio"]
