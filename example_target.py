import time
import random

print("[Target] Starting simulation...")
step = 0
entropy_level = 100.0

while True:
    step += 1
    entropy_level += random.uniform(0.5, 2.5)
    world_state = "Stable" if entropy_level < 150 else "Chaos"
    
    print(f"[Target] Step {step} | Entropy: {entropy_level:.2f} | State: {world_state}")
    time.sleep(2)
