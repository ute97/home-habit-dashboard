FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apk add --no-cache su-exec tzdata

COPY server.py /app/server.py
COPY www /app/www
COPY run.sh /run.sh

RUN addgroup -S app && adduser -S -G app app \
    && chmod a+x /run.sh \
    && chown -R app:app /app

EXPOSE 8099
CMD ["/run.sh"]
