FROM python:3.12-slim

WORKDIR /app

# Ensure Python output is sent straight to stdout without buffering
ENV PYTHONUNBUFFERED=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy operator code
COPY main.py .
COPY resources.py .

# Run as non-root user
RUN useradd -r -u 1000 operator -g operator
USER operator

# Run the operator
CMD ["kopf", "run", "main.py", "--verbose", "--all-namespaces"]
