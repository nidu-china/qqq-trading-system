FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json /web/
RUN npm install
COPY frontend /web
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

RUN addgroup --system trader && adduser --system --ingroup trader --home /home/trader trader
WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY sql /app/sql
COPY --from=web /web/dist /app/frontend/dist

RUN pip install --no-cache-dir .

RUN mkdir -p /data/market /data/reports /home/trader/.longbridge/openapi/tokens \
    && chown -R trader:trader /data /home/trader /app

USER trader
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec qqq-trader trade"]
