FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

COPY requirements.txt requirements-facebook.txt ./
RUN python -m pip install --no-cache-dir -r requirements-facebook.txt

COPY flatwatch ./flatwatch
COPY config.json README.md ./

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "flatwatch", "run"]
