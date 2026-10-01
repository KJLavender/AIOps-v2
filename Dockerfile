FROM python:3.12-slim

# kubectl: agents talk to the API with their own ServiceAccount (least-privilege RBAC).
ARG KUBECTL_VERSION=v1.31.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY aiops/ ./aiops/
COPY agents/ ./agents/

# Read-only root filesystem: kubectl's cache and Python's bytecode go to /tmp.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
USER 1000
ENTRYPOINT ["python", "-m", "agents"]
