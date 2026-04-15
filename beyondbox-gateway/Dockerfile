ARG BUILD_FROM
FROM $BUILD_FROM

# Install telegraf from InfluxData apt repo
RUN \
    apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg jq && \
    curl -fsSL https://repos.influxdata.com/influxdata-archive_compat.key \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/influxdata.gpg && \
    echo "deb https://repos.influxdata.com/debian stable main" \
        > /etc/apt/sources.list.d/influxdata.list && \
    apt-get update && \
    apt-get install -y telegraf && \
    apt-get remove -y curl gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY telegraf.conf.tpl .
COPY client.py .

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
