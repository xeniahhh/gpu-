FROM node:22-alpine AS web-build

WORKDIR /app/apps/web
RUN corepack enable
COPY apps/web/package.json apps/web/pnpm-lock.yaml apps/web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/web/ ./
RUN pnpm build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages/fabric-domain/src:/app/packages/event-contracts/src:/app/services/gateway/src:/app/services/planner/src:/app/services/aggregator/src:/app/services/evaluator/src

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi>=0.115,<1.0" \
    "httpx>=0.28,<1.0" \
    "nats-py>=2.10,<3.0" \
    "psycopg[binary]>=3.2,<4.0" \
    "python-dotenv>=1.0,<2.0" \
    "uvicorn[standard]>=0.34,<1.0"

COPY packages/ packages/
COPY services/ services/
COPY scripts/ scripts/
COPY db/ db/
COPY tests/fixtures/ tests/fixtures/
COPY --from=web-build /app/apps/web/dist/ apps/web/dist/

EXPOSE 8000

CMD ["python", "scripts/start_gateway.py"]
