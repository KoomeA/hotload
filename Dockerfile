FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 hotload
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
USER hotload
ENV HOTLOAD_DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "hotload.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
