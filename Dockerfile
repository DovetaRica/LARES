FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/home-ai
COPY pyproject.toml README.md ./
COPY home_ai ./home_ai
RUN pip install --no-cache-dir ".[ha]" && useradd --uid 10001 --no-create-home homeai
COPY examples ./examples
COPY config/example.json ./config/example.json
USER homeai
ENTRYPOINT ["python", "-m", "home_ai"]
CMD ["demo"]
