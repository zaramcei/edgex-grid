#!/bin/bash

export EDGEX_BASE_URL=https://pro.edgex.exchange
export EDGEX_CONTRACT_ID=10000001
export EDGEX_GRID_LEVELS_PER_SIDE=10
export EDGEX_GRID_STEP_USD=10
export EDGEX_GRID_FIRST_OFFSET_USD=10
export EDGEX_GRID_SIZE=0.002
export EDGEX_GRID_OP_SPACING_SEC=0.1
export EDGEX_LEVERAGE=100 # new
export EDGEX_BALANCE_RECOVERY_ENABLED=true # new
export EDGEX_INITIAL_BALANCE_USD=1743.0 # new
export EDGEX_RECOVERY_ENFORCE_LEVEL_USD=3.0 # new
export EDGEX_USE_SCHEDULE_TYPE=aggressive
export EDGEX_USE_SCHEDULE=true # new: schedule feature enabled
export EDGEX_OUT_OF_SCHEDULE_ACTION=auto # new: action when out of schedule (nothing/auto/immediately)
# export EDGEX_POSITION_SIZE_LIMIT_BTC=0.01 # new: REDUCE_MODE threshold (BTC)
# export EDGEX_POSITION_SIZE_REDUCE_ONLY_BTC=0.004 # new: REDUCE_MODE release threshold (BTC)
export EDGEX_POSITION_SIZE_LIMIT_RATIO=0.5 # new: REDUCE_MODE threshold (RATIO %)
export EDGEX_POSITION_SIZE_REDUCE_ONLY_RATIO=0.25 # new: REDUCE_MODE release threshold (RATIO %)
export EDGEX_ACCOUNT_ID=${EDGEX_ACCOUNT_ID}
export EDGEX_STARK_PRIVATE_KEY=${EDGEX_STARK_PRIVATE_KEY}

uv run run_edgex_grid.py