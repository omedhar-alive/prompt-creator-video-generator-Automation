import os, base64
from dotenv import load_dotenv
import os

load_dotenv()

print("RUNWAY KEY:", os.getenv("RUNWAYML_API_SECRET"))
from runwayml import RunwayML, TaskFailedError

RUNWAYML_API_SECRET = os.getenv("RUNWAYML_API_SECRET")
if not RUNWAYML_API_SECRET:
    raise SystemExit("Missing RUNWAYML_API_SECRET in env/.env")

client = RunwayML()  # SDK reads env by default

image_path = "/Users/omar/Desktop/ai-video-factory/uploads/FitFuel/0_UI.png"

with open(image_path, "rb") as f:
    b64 = base64.b64encode(f.read()).decode("utf-8")

# Use the correct mime for your file if not png
data_uri = f"data:image/png;base64,{b64}"

try:
    task = (
        client.image_to_video.create(
            model="gen4_turbo",
            prompt_image=data_uri,
            prompt_text="Cinematic product hero shot, smooth camera move, premium lighting, 5 seconds",
            ratio="1280:720",
            duration=5,
        )
        .wait_for_task_output()
    )
    print("DONE. Output:", task.output)
except TaskFailedError as e:
    print("FAILED.")
    print(e.task_details)