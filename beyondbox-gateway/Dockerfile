FROM telegraf:1.30

# Add Python3 for the BeyondBox management client
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        jq \
    && pip3 install requests --break-system-packages \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY telegraf.conf.tpl .
COPY client.py .
COPY requirements.txt .

COPY run.sh /run.sh
RUN chmod +x /run.sh

USER root
CMD ["/run.sh"]
