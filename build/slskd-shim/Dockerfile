FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask waitress requests slskd-api
COPY slskd_shim.py .
CMD ["python", "-u", "slskd_shim.py"]
