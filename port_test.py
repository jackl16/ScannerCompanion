import serial
import time

# Connect to the transmission side of the virtual bridge
# On Linux/macOS use: '/dev/pts/1'
SIMULATOR_PORT = 'COM5' 
BAUD_RATE = 9600

try:
    ser = serial.Serial(SIMULATOR_PORT, BAUD_RATE, timeout=1)
    print(f"Scanner emulator active on {SIMULATOR_PORT}...")
    
    while True:
        barcode = input("Enter mock barcode to send (or 'q' to quit): ")
        if barcode.lower() == 'q':
            break
            
        # Physical scanners append a suffix (usually \r, \n, or \r\n)
        payload = f"{barcode}\r\n" 
        
        # Send data as bytes
        ser.write(payload.encode('utf-8'))
        print(f"Sent: {barcode!r} over serial bridge.")
        
    ser.close()
except serial.SerialException as e:
    print(f"Error opening port {SIMULATOR_PORT}. Make sure your virtual port software is running: {e}")
