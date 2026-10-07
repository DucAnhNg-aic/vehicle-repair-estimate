"""Chạy HTTP API: python -m repair_estimate_api."""
import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "repair_estimate_api.app:app",
        host=os.getenv("VRE_API_HOST", "127.0.0.1"),
        port=int(os.getenv("VRE_API_PORT", "8000")),
    )
