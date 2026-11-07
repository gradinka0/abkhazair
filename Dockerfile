FROM python:3.12-slim
WORKDIR /app
COPY server.py flights.json ./
COPY public ./public
ENV ABKHAZ_DATA_DIR=/data
EXPOSE 8080
VOLUME ["/data"]
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]
