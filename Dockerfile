FROM python:3.12-slim AS base

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

ENV DB_PATH=/data/app.db
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# 演示/冒烟用的镜像：额外装 httpx，跑 scripts/ 下的现场试跑脚本。
FROM base AS demo

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
