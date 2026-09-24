# The venue scoreboard listener, for running in a container.
#
#   docker build -t pinbot-display display/
#   docker run -d --restart unless-stopped -p 8080:8080 \
#       --env-file display.env --name pinbot-display pinbot-display
#
# No credentials are baked in: they arrive at run time via --env-file.
FROM python:3.12-slim

RUN pip install --no-cache-dir "paho-mqtt>=1.6"

WORKDIR /app
COPY server.py index.html ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
USER nobody
CMD ["python", "server.py", "--env-file", "/dev/null"]
