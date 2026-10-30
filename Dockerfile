FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 hotload
WORKDIR /app
COPY pyproject.toml constraints.txt README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt .
COPY --from=web /web/build /app/web
USER hotload
ENV HOTLOAD_DATA_DIR=/data HOTLOAD_WEB_DIR=/app/web
EXPOSE 8000
CMD ["uvicorn", "hotload.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
