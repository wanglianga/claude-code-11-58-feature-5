FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
# tsinghua 镜像对部分包返回 403,统一使用阿里云镜像
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY app ./app
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

ENV DATABASE_URL=postgresql+psycopg2://ccdev:ccdev@db:5432/ccdev \
    SEED_ON_START=1
EXPOSE 8000
CMD ["./entrypoint.sh"]
