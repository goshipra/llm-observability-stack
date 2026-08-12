FROM python:3.12-slim

WORKDIR /app
COPY cost_exporter.py .

# No third-party dependencies — deliberately kept to the stdlib (urllib, http.server)
# so this stays a ~60-line, easy-to-audit exporter rather than a framework project.
EXPOSE 9105

ENTRYPOINT ["python3", "cost_exporter.py"]
