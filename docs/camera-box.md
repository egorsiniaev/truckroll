# Camera box: Raspberry Pi 3B + Logitech webcam

Goal: a live view of the desk charger on the public page. The Pi pushes one video stream out to the server; nothing
connects in to the home network. The camera sees only the charger diorama, and there's no audio.

```
[webcam] -USB-> [Pi 3B: ffmpeg, H.264 in hardware] --SRT, encrypted, outbound--> [Hetzner: MediaMTX] --WebRTC/HLS--> page
```

Time: about 1.5 h. You need: Pi 3B, official 5 V 2.5 A supply, microSD 16-32 GB (A1 or better), the webcam,
and a small stand or clamp for it.

## 1. Flash the OS

1. Install **Raspberry Pi Imager** on the Mac (`brew install --cask raspberry-pi-imager`).
2. Device: Raspberry Pi 3. OS: **Raspberry Pi OS Lite (64-bit)**. Use Lite: no desktop, so the Pi 3B's 1 GB stays
   free for video.
3. Edit settings before writing:
   - Hostname `truckroll-cam`.
   - A user name of your choice, with a password stored in 1Password.
   - Wi-Fi: your SSID, country `NL`.
   - Locale: `Europe/Amsterdam`.
   - Services: enable SSH, **public-key only**, and paste your Mac's public key (`cat ~/.ssh/id_ed25519.pub`).
4. Write the card, insert it, power on. First boot takes about 2 min.
5. From the Mac: `ssh <user>@truckroll-cam.local`.

## 2. Base setup

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y ffmpeg v4l-utils unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades   # security updates on their own
echo "dtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt   # Bluetooth off
sudo reboot
```

Check power after the reboot: `vcgencmd get_throttled` must print `throttled=0x0`. Anything else means the supply
or cable is too weak, and the video will stutter.

## 3. Check the webcam

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
```

Look for what it offers at 1280x720:

- **H264**: older C920s encode on the camera. Best case: the Pi only forwards the stream.
- **MJPG**: most webcams. The Pi decodes it and re-encodes to H.264 in hardware. Fine at 720p and 15 fps.
- Only **YUYV**: USB 2 limits this to about 10 fps at 720p. Use 640x480 instead.

Lock focus and exposure, so the image doesn't pulse with the LED ring, and set 50 Hz against flicker:

```bash
v4l2-ctl -d /dev/video0 --list-ctrls        # names differ a bit per model
v4l2-ctl -d /dev/video0 -c power_line_frequency=1 -c focus_automatic_continuous=0 -c auto_exposure=1
```

## 4. Test recording (no server needed)

```bash
ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 15 -i /dev/video0 \
  -c:v h264_v4l2m2m -b:v 1500k -g 30 -an -t 10 test.mp4
```

Copy it to the Mac and look at the framing: `scp <user>@truckroll-cam.local:test.mp4 .`

- If `h264_v4l2m2m` fails, run `ffmpeg -encoders | grep v4l2` and send me the output.
- Watch `top` while it records: ffmpeg should stay under about 70% CPU.

Framing: the charger, the car and the grid box fill the picture. No window, screen, face or paper with personal
details in view.

## 5. Stream to the server (once Hetzner is up)

The server runs MediaMTX. The Pi publishes over **SRT** with a passphrase: encrypted, and it copes with Wi-Fi hiccups.

1. Check SRT support: `ffmpeg -protocols 2>/dev/null | grep srt`.
2. Create the stream passphrase in 1Password (at least 16 characters). The same value goes into the server's
   environment later. Never put it in git or in chat.
3. On the Pi, a root-only file that systemd reads at start:

   ```bash
   sudo install -m 600 /dev/null /etc/truckroll-cam.env
   sudo nano /etc/truckroll-cam.env
   ```

   ```
   STREAM_URL=srt://<server-name>:8890?streamid=publish:desk&pbkeylen=16
   STREAM_PASSPHRASE=<paste from 1Password>
   ```

4. The service, `/etc/systemd/system/truckroll-cam.service`:

   ```ini
   [Unit]
   Description=Truckroll desk camera
   After=network-online.target
   Wants=network-online.target

   [Service]
   EnvironmentFile=/etc/truckroll-cam.env
   ExecStart=/bin/sh -c 'exec ffmpeg -loglevel warning -f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 15 -i /dev/video0 -c:v h264_v4l2m2m -b:v 1500k -g 30 -an -f mpegts "${STREAM_URL}&passphrase=${STREAM_PASSPHRASE}"'
   Restart=always
   RestartSec=5
   DynamicUser=yes
   SupplementaryGroups=video

   [Install]
   WantedBy=multi-user.target
   ```

5. Start it, and check that it starts again by itself after a reboot:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now truckroll-cam
   journalctl -u truckroll-cam -f
   ```

## 6. Done when

- The stream plays in the browser from the server, about 1-2 s behind real time.
- Pulling the Pi's power and plugging it back in brings the stream back within a minute, with no manual step.
- `vcgencmd get_throttled` still prints `0x0` after an hour of streaming.
