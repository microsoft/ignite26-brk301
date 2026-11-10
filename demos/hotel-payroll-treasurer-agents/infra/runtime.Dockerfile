FROM node:24-bookworm-slim@sha256:51b1100cc2a83d370c6a60952e3f2989c8a43159d0e38586e090f3b3326efefd AS parquet
WORKDIR /reader
COPY parquet-reader/package.json parquet-reader/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM python:3.13-slim-bookworm
WORKDIR /app
COPY --from=parquet /usr/local/bin/node /usr/local/bin/node
COPY --from=parquet /reader/node_modules /app/parquet-reader/node_modules
COPY parquet-reader/reader.mjs /app/parquet-reader/reader.mjs
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir ".[host]"
ENV PYTHONPATH=/app/src
ENV PORT=8088
EXPOSE 8088
ENTRYPOINT ["python", "-m", "hotel_payroll_treasurer_agents.host"]
