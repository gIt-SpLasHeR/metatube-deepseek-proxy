FROM python:3.12-alpine
WORKDIR /app
COPY proxy.py report.py ./
COPY tools ./tools
ENV LISTEN_HOST=0.0.0.0 LISTEN_PORT=8765 THINKING=disabled USAGE_LOG=/data/usage.jsonl PYTHONUNBUFFERED=1
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status == 200 else 1)"
CMD ["python", "proxy.py"]
