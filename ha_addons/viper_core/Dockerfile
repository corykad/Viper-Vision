ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache ffmpeg python3

WORKDIR /app
COPY viper_core /app/viper_core
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
