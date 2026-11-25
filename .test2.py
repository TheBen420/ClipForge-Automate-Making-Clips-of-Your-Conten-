import sys, time
sys.path.insert(0, "C:/Users/Ben/clipforge")
sys.argv = ["x"]
import tkinter as tk
import clipforge_gui as g

LOG = open("C:/Users/Ben/clipforge/.test2.log", "w", encoding="utf-8", buffering=1)

def note(line):
    LOG.write(line)
    LOG.flush()

root = tk.Tk()
root.withdraw()
app = g.App(root)

app.nb.select(1)
app.vod_url_var.set("https://www.twitch.tv/videos/2917783902")
app.count_var.set("10")
app.length_var.set("25")
app.chat_var.set(True)
app.preset_var.set("auto")
app.workers_var.set("2")
app.fps_var.set("auto")
app.model_var.set("medium.en")
app.cap_var.set("karaoke")
app.output_var.set("D:/ClipForge/clips")
app.subfolder_var.set(True)

app.load_vod()
deadline = time.time() + 180
while app.vod_info is None and time.time() < deadline:
    root.update()
    time.sleep(0.2)
note(f"VOD: {app.vod_info_var.get()}\n")

started = time.time()
app.start()
last = 0.0
while app.running and time.time() - started < 5400:
    root.update()
    time.sleep(0.25)
    if time.time() - last > 20:
        last = time.time()
        try:
            text = app.log.get("1.0", "end")
        except tk.TclError:
            break
        if text.strip():
            note(f"[{int(time.time()-started)}s] {text.strip().splitlines()[-1][:170]}\n")

note(f"\nelapsed: {time.time()-started:.0f}s  clips: {len(app.clips_made)}  dir: {app.run_dir}\n")
for index in range(app.review_list.size()):
    note("  clip: " + app.review_list.get(index) + "\n")
try:
    root.destroy()
except tk.TclError:
    pass
LOG.close()
