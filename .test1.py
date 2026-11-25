import sys, time
sys.path.insert(0, "C:/Users/Ben/clipforge")
sys.argv = ["x"]
import tkinter as tk
import clipforge_gui as g

LOG = open("C:/Users/Ben/clipforge/.test1.log", "w", encoding="utf-8", buffering=1)

def note(line):
    LOG.write(line)
    LOG.flush()

root = tk.Tk()
root.withdraw()
app = g.App(root)

app.nb.select(0)
app.folder_var.set(r"D:\Palworld Good Ultra Boss Kills")
app.count_var.set("2")
app.length_var.set("25")
app.chat_var.set(False)
app.preset_var.set("auto")
app.workers_var.set("2")
app.fps_var.set("auto")
app.model_var.set("medium.en")
app.cap_var.set("karaoke")
app.output_var.set("D:/ClipForge/clips")
app.subfolder_var.set(True)

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
