FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

COPY app ./app
COPY scripts ./scripts

EXPOSE 8791

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8791", "--no-access-log"]
