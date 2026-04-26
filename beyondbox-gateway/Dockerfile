FROM telegraf:1.30 AS telegraf-binary
FROM python:3.12-slim

# Copy telegraf binary from official image (no apt repo setup needed)
COPY --from=telegraf-binary /usr/bin/telegraf /usr/local/bin/telegraf

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegraf.conf.tpl .
COPY client.py .

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
