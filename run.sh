#!/bin/bash

docker run -it --rm \
  --name edgex-bot-container \
  -e EDGEX_BASE_URL=https://pro.edgex.exchange \
  -e EDGEX_CONTRACT_ID=10000001 \
  -e EDGEX_GRID_LEVELS_PER_SIDE=6 \
  -e EDGEX_GRID_STEP_USD=60 \
  -e EDGEX_GRID_FIRST_OFFSET_USD=60 \
  -e EDGEX_GRID_SIZE=0.001 \
  -e EDGEX_GRID_OP_SPACING_SEC=1.5 \
  -e EDGEX_ACCOUNT_ID=${EDGEX_ACCOUNT_ID} \
  -e EDGEX_STARK_PRIVATE_KEY=${EDGEX_STARK_PRIVATE_KEY} \
  -v $(pwd)/logs:/app/logs \
  edgex-bot