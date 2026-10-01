FROM python:3.12-slim

RUN useradd --create-home reaper
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER reaper
ENTRYPOINT ["grimreaper"]
CMD ["--help"]
