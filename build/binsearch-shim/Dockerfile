FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask waitress requests
COPY binsearch_shim.py .
CMD ["python", "-u", "binsearch_shim.py"]
