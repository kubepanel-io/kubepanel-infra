# cPanel Migration Worker
# This image runs as a Kubernetes Job to process migration batches

FROM python:3.13.1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg2 \
    apt-transport-https \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl (download binary directly to avoid apt repo signature issues)
RUN curl -LO "https://dl.k8s.io/release/v1.29.0/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

# Create app directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy worker script
COPY worker.py .

# KubePanel dashboard code is mounted from the cluster's kubepanel PVC at /kubepanel
# The migration job spec includes podAffinity to run on the same node as kubepanel pod
# This ensures access to the ReadWriteOnce PVC containing Django models and services
ENV PYTHONPATH=/kubepanel

# Note: Container runs as root via K8s securityContext for shared PVC access

CMD ["python", "worker.py"]
