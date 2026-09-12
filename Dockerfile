# ------------------------------------------------------------------------------
# JARVIS (Mark XLVIII) - Headless Server & Mobile Gateway Container
# ------------------------------------------------------------------------------
FROM python:3.12-slim

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV HEADLESS=1

WORKDIR /app

# 1. Install system dependencies: audio libraries, build tools, Node.js & npm (for MCP servers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    libasound2-dev \
    libportaudio2 \
    libportaudiocpp0 \
    portaudio19-dev \
    ffmpeg \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy dependencies and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir fastapi "uvicorn[standard]" cryptography websockets mcp

# 3. Copy application source code
COPY . .

# 4. Expose the Remote Dashboard port (WebSocket audio & PWA interface)
EXPOSE 8000

# 5. Launch JARVIS in Headless Server mode
CMD ["python", "main.py", "--headless"]
