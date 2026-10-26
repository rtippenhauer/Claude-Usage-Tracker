FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
COPY server server
COPY dashboard dashboard
COPY analyzers analyzers
COPY agents agents
ENV TRACKER_DB=/data/tracker.db
EXPOSE 8787
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8787"]
