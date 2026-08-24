FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests
COPY dashboard_stats.py purge_lib.py reaper.py reaper_retry.py security_mon.py abuse_report.py ./
CMD ["python", "-u", "dashboard_stats.py"]
