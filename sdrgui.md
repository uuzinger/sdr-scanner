Markdown
# Trunk Recorder Appliance Dashboard

This project turns an Ubuntu machine running [Trunk Recorder](https://github.com/robotastic/trunk-recorder) into a dedicated hardware appliance. It utilizes a lightweight, hardware-accelerated Python (Pygame) frontend to display live radio traffic, system telemetry, and a rolling log feed directly to a physical display—all without requiring a heavy desktop environment like GNOME or KDE.

It is specifically optimized for 2U ultra-wide rackmount displays (e.g., 1920x480 natively mapped to a 1080p signal).

## Features
* **Zero-Desktop Overhead:** Runs purely on X11 (`xinit`) directly from the terminal buffer.
* **Least-Privilege Architecture:** The UI runs under an unprivileged `sdr-kiosk` user that cannot access the system shell.
* **AVX2 Acceleration:** Uses a source-compiled version of Pygame with bilinear filtering to smoothly scale the virtual canvas to ultra-wide displays with minimal CPU usage.
* **Live Telemetry:** Tracks CPU, Memory, and Network I/O.
* **Regex Log Tailing:** Automatically detects and tails the newest rolling log files from Trunk Recorder to extract Talkgroups, Frequencies, and plain-text Unit Aliases.

---

## Phase 1: Dependencies & Optimized Pygame

1. **Install System Dependencies**
   You need `acl` for fine-grained log permissions, and the SDL2 headers to compile Pygame from source.
   ```bash
   sudo apt update
   sudo apt install acl python3-psutil python3-dev libsdl2-dev libsdl2-image-dev libsdl2-ttf-dev xorg xserver-xorg-video-fbdev
Compile AVX2-Optimized Pygame
To keep CPU usage under 3% while scaling the 2U display, compile Pygame with AVX2 extensions enabled:

Bash
sudo PYGAME_DETECT_AVX2=1 pip3 install pygame --force-reinstall --no-binary pygame --break-system-packages
Phase 2: Security & Permissions
Create an isolated user to run the dashboard. This ensures the frontend display cannot be used to drop into a root shell.

Create the Kiosk User

Bash
sudo adduser --disabled-password --gecos "" sdr-kiosk
Grant Log Access (ACL)
The sdr-kiosk user needs read-only access to Trunk Recorder's log directory. Replace youruser with the username running Trunk Recorder.

Bash
# Allow directory traversal
sudo setfacl -m u:sdr-kiosk:x /home/youruser
sudo setfacl -m u:sdr-kiosk:x /home/youruser/trunk-build

# Apply read permissions to existing logs
sudo setfacl -R -m u:sdr-kiosk:rx /home/youruser/trunk-build/logs

# Set default permissions so future logs inherit access automatically
sudo setfacl -d -m u:sdr-kiosk:rx /home/youruser/trunk-build/logs
Phase 3: The Dashboard Application
Create the Application Directory

Bash
sudo mkdir -p /opt/sdr-dashboard
Deploy the Script
Save the Python script as /opt/sdr-dashboard/sdr_gui.py.
(Ensure you update the LOG_DIR variable inside the script to match your Trunk Recorder logs path).

Set Ownership

Bash
sudo chown -R sdr-kiosk:sdr-kiosk /opt/sdr-dashboard
Phase 4: X11 & Systemd Configuration
1. Allow X11 Startup
By default, Ubuntu restricts X11 to physical console logins. Allow the background service to start it:

Bash
sudo nano /etc/X11/Xwrapper.config
Set the following line:

allowed_users=anybody
2. Trunk Recorder Backend Service
Ensure your SDR software starts automatically.
sudo nano /etc/systemd/system/trunk-recorder.service

[Unit]
Description=Trunk Recorder SDR Service
After=network.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/trunk-build
ExecStart=/home/youruser/trunk-build/trunk-recorder --config=config.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
3. Pygame Frontend UI Service
Run the dashboard seamlessly over the login prompt on the physical display.
sudo nano /etc/systemd/system/sdr-ui.service

[Unit]
Description=SDR Pygame Dashboard
After=systemd-user-sessions.service systemd-logind.service trunk-recorder.service
Conflicts=getty@tty1.service

[Service]
User=sdr-kiosk
WorkingDirectory=/opt/sdr-dashboard

# Take control of the physical screen
StandardInput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes

# Launch X11 on Virtual Terminal 1 (vt1)
ExecStart=/usr/bin/xinit /usr/bin/python3 /opt/sdr-dashboard/sdr_gui.py -- :0 vt1 -nocursor

Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
Phase 5: Launch!
Reload the systemd daemon, enable both services to run on boot, and start them:

Bash
sudo systemctl daemon-reload
sudo systemctl enable trunk-recorder sdr-ui
sudo systemctl start trunk-recorder sdr-ui
Your 2U screen will instantly take over the terminal buffer and display the live dashboard.
