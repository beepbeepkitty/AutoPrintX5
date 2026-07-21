import asyncio
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ImageEnhance
from bleak import BleakScanner, BleakClient

# GATT UUIDs for X5 / Cat-style printers
WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
PRINTER_WIDTH = 384  # Standard 384 dots (48 bytes per line)

TARGET_SERVICE_UUIDS = [
    "0000af30-0000-1000-8000-00805f9b34fb",
    "0000ae30-0000-1000-8000-00805f9b34fb",
]

# Pre-calculated bit reversal table
BIT_REVERSE_TABLE = bytes([
    int(f'{i:08b}'[::-1], 2) for i in range(256)
])

def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc

def make_command(cmd: int, payload: bytes = b'') -> bytes:
    length = len(payload)
    len_lo = length & 0xFF
    len_hi = (length >> 8) & 0xFF
    header = bytes([0x51, 0x78, cmd, 0x00, len_lo, len_hi])
    chk = crc8(payload)
    return header + payload + bytes([chk, 0xFF])

class TinyPrinterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AutoPrintX5")
        self.root.geometry("400x580")
        self.root.resizable(False, False)

        self.selected_image_path = None
        self.preview_image = None

        self.btn_select = tk.Button(root, text="Select Image", command=self.select_image, font=("Arial", 12))
        self.btn_select.pack(pady=10)

        self.canvas = tk.Canvas(root, width=384, height=270, bg="#f0f0f0", relief="sunken", bd=1)
        self.canvas.pack(pady=5)

        # Brightness Slider Control (1.0 = Default, >1.0 = Lighter/Brightened)
        self.lbl_bright = tk.Label(root, text="Image Brightness Boost:", font=("Arial", 10))
        self.lbl_bright.pack(pady=(5, 0))
        self.slider_bright = tk.Scale(root, from_=0.5, to=2.5, resolution=0.1, orient=tk.HORIZONTAL, length=300, command=self.update_preview)
        self.slider_bright.set(1.3)  # Default to 1.3 for lighter prints
        self.slider_bright.pack(pady=(0, 5))

        self.lbl_status = tk.Label(root, text="No image selected", font=("Arial", 10), fg="gray")
        self.lbl_status.pack(pady=5)

        self.btn_print = tk.Button(root, text="Print Image", command=self.start_print_thread, state=tk.DISABLED, bg="#4CAF50", fg="white", font=("Arial", 12, "bold"))
        self.btn_print.pack(pady=10)

    def select_image(self):
        file_types = [("Image Files", "*.png *.jpg *.jpeg *.bmp"), ("All Files", "*.*")]
        file_path = filedialog.askopenfilename(title="Select Image to Print", filetypes=file_types)

        if file_path:
            self.selected_image_path = file_path
            self.update_preview()
            self.lbl_status.config(text=f"Selected: {file_path.split('/')[-1]}", fg="black")
            self.btn_print.config(state=tk.NORMAL)

    def update_preview(self, val=None):
        if not self.selected_image_path:
            return
        
        img = Image.open(self.selected_image_path)
        
        # Apply current brightness setting to preview
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(self.slider_bright.get())
        
        img.thumbnail((380, 260))
        self.preview_image = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(192, 135, image=self.preview_image)

    def build_print_job(self, image_path, target_width=PRINTER_WIDTH) -> bytes:
        img = Image.open(image_path).convert('L')
        
        # 1. Apply user brightness boost before dithering
        brightness_factor = self.slider_bright.get()
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(brightness_factor)

        # 2. Aspect-ratio resize
        w_percent = target_width / float(img.size[0])
        target_height = int(float(img.size[1]) * float(w_percent))
        
        img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
        img_bw = img.convert('1', dither=Image.FLOYDSTEINBERG)

        raw_bytes = img_bw.tobytes()
        bytes_per_line = target_width // 8

        job_data = bytearray()

        # Initialization sequence
        job_data.extend(make_command(0xA1, b'\x01'))          # Speed/Quality
        job_data.extend(make_command(0xBE, b'\x00\x00'))      # Start Session
        
        # LOWER HEAT ENERGY: 0x20 (32 decimal) instead of 0x40 (64)
        job_data.extend(make_command(0xAF, b'\x00\x20'))      

        # Line-by-line raster packet creation
        for y in range(target_height):
            start_idx = y * bytes_per_line
            line = raw_bytes[start_idx : start_idx + bytes_per_line]
            
            # Bit-inversion & Bit-reversal
            processed_line = bytes(BIT_REVERSE_TABLE[~b & 0xFF] for b in line)
            job_data.extend(make_command(0xA2, processed_line))

        # Paper feed & end session
        job_data.extend(make_command(0xA1, b'\x50'))          
        job_data.extend(make_command(0xA3, b'\x01'))          
        
        return bytes(job_data)

    async def print_image_async(self):
        self.lbl_status.config(text="Scanning for printer...", fg="blue")
        self.btn_print.config(state=tk.DISABLED)

        devices_and_adv = await BleakScanner.discover(return_adv=True)
        printer_device = None

        for device, adv_data in devices_and_adv.values():
            adv_uuids = [u.lower() for u in adv_data.service_uuids]
            
            uuid_match = any(t in adv_uuids for t in TARGET_SERVICE_UUIDS)
            name_match = device.name and any(k in device.name for k in ["X5", "MX", "Printer", "Tiny", "Cat"])

            if uuid_match or name_match:
                printer_device = device
                break

        if not printer_device:
            messagebox.showerror("Error", "Printer not found!")
            self.lbl_status.config(text="Printer not found", fg="red")
            self.btn_print.config(state=tk.NORMAL)
            return

        self.lbl_status.config(text=f"Connecting to {printer_device.name or printer_device.address}...", fg="blue")

        try:
            payload = self.build_print_job(self.selected_image_path)
            
            async with BleakClient(printer_device.address) as client:
                self.lbl_status.config(text="Sending print command...", fg="green")
                
                chunk_size = 128
                for i in range(0, len(payload), chunk_size):
                    chunk = payload[i:i + chunk_size]
                    await client.write_gatt_char(WRITE_UUID, chunk, response=False)
                    await asyncio.sleep(0.02)

                messagebox.showinfo("Success", "Printing finished successfully!")
                self.lbl_status.config(text="Print finished!", fg="green")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to print: {str(e)}")
            self.lbl_status.config(text="Printing failed", fg="red")

        finally:
            self.btn_print.config(state=tk.NORMAL)

    def start_print_thread(self):
        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.print_image_async())

        threading.Thread(target=run_loop, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = TinyPrinterApp(root)
    root.mainloop()