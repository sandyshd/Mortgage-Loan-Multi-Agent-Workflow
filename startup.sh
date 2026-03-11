#!/bin/bash
# App Service startup script – installs dependencies and starts uvicorn
pip install -r requirements.txt
uvicorn src.api.main:app --host 0.0.0.0 --port 8000
