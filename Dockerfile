FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg nodejs
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
# Render uses the $PORT environment variable
CMD uvicorn server:app --host 0.0.0.0 --port $PORT
