import sys
from pathlib import Path
import json

# Add actions to path
sys.path.append(str(Path(__file__).parent))

from actions.edith_agent import edith_agent

class MockPlayer:
    def write_log(self, msg):
        print(f"LOG: {msg}")

def test_connectivity():
    print("Testing EDITH connectivity...")
    player = MockPlayer()
    params = {"action": "verify", "task": "Hello EDITH, can you hear me?"}
    
    try:
        result = edith_agent(parameters=params, player=player)
        print(f"Result: {result}")
    except Exception as e:
        print(f"Error during test: {e}")

if __name__ == "__main__":
    test_connectivity()
