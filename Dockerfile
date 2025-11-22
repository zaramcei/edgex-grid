FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends cmake make gcc g++ curl libgmp-dev ca-certificates && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin/:$PATH"

COPY . .

RUN uv sync --frozen

# Copy customized edgex_sdk over the installed version
RUN cp -r /app/local-packages/edgex_sdk /app/.venv/lib/python3.11/site-packages/

RUN mkdir -p logs

ENV EDGEX_BASE_URL=https://pro.edgex.exchange
ENV EDGEX_CONTRACT_ID=10000001
ENV EDGEX_GRID_LEVELS_PER_SIDE=6
ENV EDGEX_GRID_STEP_USD=60
ENV EDGEX_GRID_FIRST_OFFSET_USD=60
ENV EDGEX_GRID_SIZE=0.2
ENV EDGEX_GRID_OP_SPACING_SEC=1.5

CMD ["uv", "run", "run_edgex_grid.py"]