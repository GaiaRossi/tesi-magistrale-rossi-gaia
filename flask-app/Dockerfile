FROM python:3.10-alpine AS builder

WORKDIR /app

COPY requirements.txt /app
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install -r requirements.txt

COPY ./simple-app.py /app

ENTRYPOINT ["python3"]
CMD ["simple-app.py"]