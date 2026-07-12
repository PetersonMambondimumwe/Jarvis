import sys
import os
from pathlib import Path

# Add the project root to the path so we can import dashboard.server
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from dashboard.server import DashboardServer

# Initialize the server instance
# Note: In a serverless environment, this instance will be recreated per request
# Persistent state like _clients or _history will not be shared across requests
# unless using an external database or cache.
server = DashboardServer()

# Expose the FastAPI app instance for Vercel
app = server.app
