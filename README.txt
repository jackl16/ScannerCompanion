### Center Scanner Companion

## User & Setup Guide

Scanner Companion is a lightweight, high-performance desktop utility designed to automate student attendance check-ins and progress logging. The application handles local barcode scanner connections automatically and syncs data safely to the cloud database. 

### How to Install and Run

The application is completely self-contained. You do not need to install any additional software.

1. **Download the App**: Copy the folder containing ScannerCompanion.exe onto your center's main computer that will conduct the scans.
2. **Keep Files Together**: Make sure the scanner config file (scanner_config.toml) stays in the exact same folder as the .exe file.
3. **Launch**: Double-click ScannerCompanion.exe to launch the app.
4. **Login**: Login to the app using the given credentials.
5. **Connect Scanners**: Go to the settings menu drop-down and connect all desired scanners 
6. **Scan**: The scanner should now connect with the database

## Daily Operational Guide

### Checking Connection Health

* Look at the **Status Bar** running along the very bottom row of the application window.
* **✅ Connected**: The application has successfully claimed your USB hardware slots and is ready to process student workpacks.
* **… Processing**: A barcode was read and the system is communicating securely with the cloud servers.
* **❌ Disconnected**: Connection lost. Check your scanner cables or restart the tool.

### Maintenance Tools

* **Clear Scan Log**: Click **System -> Clear Scan Log** in the top menu bar to wipe the active onscreen history tracker cleanly if it gets too crowded.
* **Export Scan Log**: Click **System -> Export Scan Log** to save a text backup file of the day's raw scan data directly to your computer's desktop or a flash drive.

### Hardware Troubleshooting Checklist
** Hardware Scanners MUST have a 'COM' mode, 'HID/Keyboard' mode scanners will NOT work.**

If your physical barcode reader isn't registering scans or the bottom bar shows a red "❌", follow these simple steps to try to fix the issue(s): 

1. **The 3-Second Rule**: Unplug the scanner's USB cable from the back of the computer tower, wait 3 seconds, and plug it back into a different USB slot.
2. **Re-Detect Scanner**: In the top menu layout bar, click **Settings -> Detect Scanner** to force the app background threads to scan for hardware.
3. **Software Interference**: Ensure no other older attendance panels, command prompt terminals, or barcode configuration software sheets are running in the background locking up the ports.


### Additional Notes & Contact
If there seems to be an error with the database or any other concerns please contact me at jackli6140@gmail.com.



### Legal Disclaimer
This software is an entirely independent, third-party operational utility tool. It is not officially affiliated with, endorsed by, authorized by, or sponsored by Kumon North America, Inc., or any of its corporate franchise branches. All trademarks remain the property of their respective owners.