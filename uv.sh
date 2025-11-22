#!/bin/bash

export EDGEX_BASE_URL=https://pro.edgex.exchange
export EDGEX_CONTRACT_ID=10000001
export EDGEX_GRID_LEVELS_PER_SIDE=10
export EDGEX_GRID_STEP_USD=10
export EDGEX_GRID_FIRST_OFFSET_USD=10
export EDGEX_GRID_SIZE=0.002
export EDGEX_GRID_OP_SPACING_SEC=0.1
export EDGEX_LEVERAGE=100 # new
export EDGEX_POSITION_LOSSCUT_PERCENTAGE=10 # new: position-based loss cut
export EDGEX_POSITION_TAKE_PROFIT_PERCENTAGE=10 # new: position-based take profit
export EDGEX_BALANCE_RECOVERY_ENABLED=true # new
export EDGEX_INITIAL_BALANCE_USD=400.0 # new
export EDGEX_RECOVERY_ENFORCE_LEVEL_USD=3.0 # new
export EDGEX_ASSET_LOSSCUT_PERCENTAGE=0.05 # new: asset-based loss cut -> -0.2 usd
export EDGEX_ASSET_TAKE_PROFIT_PERCENTAGE=5.0 # new: asset-based take profit
export EDGEX_ACCOUNT_ID=${EDGEX_ACCOUNT_ID}
export EDGEX_STARK_PRIVATE_KEY=${EDGEX_STARK_PRIVATE_KEY}

uv run run_edgex_grid.py