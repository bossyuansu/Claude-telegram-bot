import subprocess
import io
import time
import sys

proc = subprocess.Popen([sys.executable, "-c", "import time; print('line1', flush=True); time.sleep(2); print('line2', flush=True)"], stdout=subprocess.PIPE)
reader = io.TextIOWrapper(proc.stdout, encoding='utf-8')
start = time.time()
for line in reader:
    print(f"Got line at {time.time() - start:.2f}s: {line.strip()}")
