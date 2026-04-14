FROM python:3.11-slim

RUN pip install --no-cache-dir requests influxdb-client

WORKDIR /app
COPY client.py .
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
