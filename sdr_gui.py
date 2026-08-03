import pygame
import psutil
import time
import os
import glob
import re
from collections import deque

# --- Configuration ---
LOG_DIR = "/home/zinger/trunk-build/logs"
LOGICAL_RES = (1920, 480) 
FPS = 10 # Reduced from 10 to drastically lower CPU usage
BG_COLOR = (15, 15, 20)      
TEXT_COLOR = (200, 200, 200) 
ACCENT_COLOR = (0, 255, 150) 
CHART_COLOR = (255, 100, 150)

# --- Data Stores ---
recent_calls = deque(maxlen=6)
raw_logs = deque(maxlen=15)
cpu_history = deque([0] * 60, maxlen=60)
net_start = psutil.net_io_counters()

# Histogram: 60 buckets representing message volume.
# We will shift a new bucket in every 2 seconds (giving a 2-minute rolling window)
msg_history = deque([0] * 60, maxlen=60)

# --- Logic Functions ---
def get_latest_log():
    list_of_files = glob.glob(f"{LOG_DIR}/*.log")
    if not list_of_files: return None
    return max(list_of_files, key=os.path.getctime)

def parse_trunk_recorder_line(line):
    log_pattern = re.compile(
        r'\[(.*?)\]\s+\((.*?)\)\s+\[(.*?)\]\s+([\w]+)\s+TG:\s+(\d+)\s+Freq:\s+([\d.]+)\s+MHz\s+(?:-\s+)?(.*)'
    )
    match = log_pattern.search(line)
    if not match: return None
    
    data = {
        "tg": match.group(5), 
        "freq": match.group(6),
        "event": match.group(7).strip()
    }
    alias_match = re.search(r'\((.*?)\)', data["event"])
    data["alias"] = alias_match.group(1) if alias_match and ("src:" in data["event"] or "alias:" in data["event"]) else ""
    return data

def draw_text(surface, text, pos, font, color=TEXT_COLOR):
    text_surface = font.render(text, True, color)
    surface.blit(text_surface, pos)

# --- Graphing Functions ---
def draw_line_graph(surface, data_deque, rect_x, rect_y, width, height, color):
    pygame.draw.rect(surface, (30, 30, 40), (rect_x, rect_y, width, height))
    for i in range(1, 4):
        pygame.draw.line(surface, (50, 50, 60), (rect_x, rect_y + (height/4)*i), (rect_x + width, rect_y + (height/4)*i))
    
    points = []
    step_x = width / (len(data_deque) - 1) if len(data_deque) > 1 else width
    for i, val in enumerate(data_deque):
        x = rect_x + (i * step_x)
        y = rect_y + height - ((val / 100.0) * height)
        points.append((x, y))
        
    if len(points) > 1:
        pygame.draw.aalines(surface, color, False, points)

def draw_timeline_histogram(surface, data_list, rect_x, rect_y, width, height, color, font):
    pygame.draw.rect(surface, (30, 30, 40), (rect_x, rect_y, width, height))
    
    # Find the peak traffic in this window to scale the bars dynamically
    max_val = max(data_list) if max(data_list) > 0 else 1
    
    # 60 buckets across the width
    bar_width = (width / len(data_list)) - 2 
    
    for i, count in enumerate(data_list):
        if count > 0:
            x = rect_x + 5 + (i * (bar_width + 2))
            bar_height = (count / max_val) * (height - 10)
            y = rect_y + height - bar_height
            pygame.draw.rect(surface, color, (x, y, bar_width, bar_height))
            
    # Print the scale in the corner
    draw_text(surface, f"Peak: {max_val} msgs", (rect_x + width - 120, rect_y + 5), font, (150, 150, 150))

def main():
    global net_start
    pygame.init()
    
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
    pygame.mouse.set_visible(False)
    actual_w, actual_h = screen.get_size()
    canvas = pygame.Surface(LOGICAL_RES)
    
    font_main = pygame.font.SysFont("mono", 24)
    font_small = pygame.font.SysFont("mono", 18)
    font_header = pygame.font.SysFont("mono", 28, bold=True)
    
    current_log_path = get_latest_log()
    log_file = open(current_log_path, 'r') if current_log_path else None
    if log_file: log_file.seek(0, 2) 
    
    last_log_check = time.time()
    last_bucket_time = time.time()
    last_cpu_check = time.time()
    clock = pygame.time.Clock()
    running = True

   
    while running:
        for event in pygame.event.get():
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE: running = False
                
        # --- 1. PROCESS TIMERS & BUCKETS ---
        # Every 2 seconds, shift the histogram array to the left (append a new 0 to the right)
        current_time = time.time()
        while current_time - last_bucket_time >= 2.0:
            msg_history.append(0)
            last_bucket_time += 2.0
            
        # Update CPU History for the graph periodically
        if current_time - last_cpu_check >= 1.0:
            cpu_history.append(psutil.cpu_percent())
            last_cpu_check = current_time
                
        # --- 2. PROCESS NEW DATA ---
        if log_file:
            line = log_file.readline()
            if line:
                clean_line = line.strip()
                raw_logs.appendleft(clean_line) 
                
                # We got a message, increment the current histogram bucket
                msg_history[-1] += 1
                
                parsed = parse_trunk_recorder_line(clean_line)
                if parsed and parsed["alias"]:
                    recent_calls.appendleft(parsed)
            else:
                if time.time() - last_log_check > 3:
                    last_log_check = time.time()
                    newest_log = get_latest_log()
                    if newest_log and newest_log != current_log_path:
                        current_log_path = newest_log
                        log_file.close()
                        log_file = open(current_log_path, 'r')

        # --- 3. DRAW UI ---
        canvas.fill(BG_COLOR)
        
        # COL 1: Hardware
        draw_text(canvas, "SYSTEM HEALTH", (20, 20), font_header, ACCENT_COLOR)
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        net_now = psutil.net_io_counters()
        rx = (net_now.bytes_recv - net_start.bytes_recv) / 1024
        tx = (net_now.bytes_sent - net_start.bytes_sent) / 1024
        net_start = net_now
        
        draw_text(canvas, f"CPU: {cpu}%", (20, 70), font_main)
        draw_text(canvas, f"MEM: {mem}%", (20, 100), font_main)
        draw_text(canvas, f"NET RX: {rx:.1f} KB/s", (20, 130), font_main)
        draw_text(canvas, f"NET TX: {tx:.1f} KB/s", (20, 160), font_main)
        
        draw_text(canvas, "CPU LOAD HISTORY", (20, 250), font_small, (150, 150, 150))
        draw_line_graph(canvas, cpu_history, 20, 275, 340, 180, ACCENT_COLOR)
        pygame.draw.line(canvas, (50, 50, 60), (380, 20), (380, 460), 2)

        # COL 2: Active Calls & Histogram
        draw_text(canvas, "RECENT ACTIVITY", (400, 20), font_header, CHART_COLOR)
        draw_text(canvas, "TG     UNIT ALIAS              FREQ", (400, 70), font_small, (150, 150, 150))
        
        y_offset = 100
        for call in recent_calls:
            row_text = f"{call['tg']:<6} {call['alias']:<23} {call['freq']}"
            draw_text(canvas, row_text, (400, y_offset), font_main)
            y_offset += 30
            
        draw_text(canvas, "TRAFFIC VOLUME (LAST 2 MIN)", (400, 310), font_small, (150, 150, 150))
        draw_timeline_histogram(canvas, msg_history, 400, 335, 680, 120, CHART_COLOR, font_small)
        
        pygame.draw.line(canvas, (50, 50, 60), (1100, 20), (1100, 460), 2)

        # COL 3: Raw Logs
        draw_text(canvas, "LIVE LOG STREAM", (1120, 20), font_header, (100, 200, 255))
        y_offset = 70
        for line in raw_logs:
            display_line = line[:75] + "..." if len(line) > 75 else line
            draw_text(canvas, display_line, (1120, y_offset), font_small)
            y_offset += 25

        # 4. Scale and Blit
        scaled_canvas = pygame.transform.smoothscale(canvas, (actual_w, actual_h))
        screen.blit(scaled_canvas, (0, 0))

        pygame.display.flip()
        clock.tick(FPS)

    if log_file: log_file.close()
    pygame.quit()

if __name__ == "__main__":
    main()
