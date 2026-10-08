FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY . /app
RUN cd /app/frontend && (npm install && npm run build)
ENV PORT=3000
EXPOSE 3000
CMD ["python", "run_demo.py"]
