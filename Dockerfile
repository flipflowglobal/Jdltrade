FROM python:3.11-slim AS base

# Install Rust for building nexus_core
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install maturin for Rust/Python bridge
RUN pip install maturin

# Build Rust extension
COPY nexus_core/ ./nexus_core/
RUN cd nexus_core && maturin develop --release

# Copy project
COPY . .

CMD ["python", "-m", "nexus_arb.orchestrator"]
