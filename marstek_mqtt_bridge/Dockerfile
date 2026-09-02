ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

# tzdata so log timestamps can follow HA's timezone; bash for run.sh niceties
RUN apk add --no-cache bash tzdata

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN chmod a+x /app/run.sh

CMD [ "/app/run.sh" ]
