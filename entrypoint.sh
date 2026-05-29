#!/bin/bash
set -euo pipefail

exec python -m app.backend --cloud "$@"
