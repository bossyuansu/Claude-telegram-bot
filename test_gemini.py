import subprocess
import io
import time
import sys

proc = subprocess.Popen(["gemini", "--prompt", "count to 5 slowly", "--output-format", "stream-json"], stdout=subprocess.PIPE)
reader = io.TextIOWrapper(proc.stdout, encoding='utf-8')
start = time.time()
for line in reader:
    print(f"Got line at {time.time() - start:.2f}s: {line.strip()[:80]}")
