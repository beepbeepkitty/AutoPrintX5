import asyncio
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk, ImageEnhance
from bleak import BleakScanner, BleakClient

# GATT Characteristics & Service UUIDs
WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
PRINTER_WIDTH = 384  # 384 dots width (58mm width, 8 dots/mm)

TARGET_SERVICE_UUIDS = [
    "0000af30-0000-1000-8000-00805f9b34fb",
    "0000ae30-0000-1000-8000-00805f9b34fb",
]

# Bit Reversal lookup table for X5 printer controller endianness
BIT_REVERSE_TABLE = bytes([int(f'{i:08b}'[::-1], 2) for i in range(256)])

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

class ProTinyPrinterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AutoPrintX5")
        self.root.geometry("450x700")
        self.root.resizable(False, False)

        self.selected_image_path = None
        self.preview_tk_img = None
        self.processed_mono_img = None

        self._build_ui()

    def _build_ui(self):
        # 1. Image Selection
        top_frame = tk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=10, pady=5)

        self.btn_select = tk.Button(top_frame, text="Select Image", command=self.select_image, font=("Arial", 10, "bold"))
        self.btn_select.pack(side=tk.LEFT, padx=5)

        self.lbl_file = tk.Label(top_frame, text="No file loaded", font=("Arial", 9), fg="gray", anchor="w")
        self.lbl_file.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # 2. Preview Canvas
        self.canvas = tk.Canvas(self.root, width=384, height=240, bg="#2b2b2b", relief="sunken", bd=1)
        self.canvas.pack(pady=5)

        # 3. Controls & Processing Settings
        controls_frame = tk.LabelFrame(self.root, text=" Processing Parameters ", font=("Arial", 9, "bold"))
        controls_frame.pack(fill=tk.X, padx=10, pady=5)

        # Dither Dropdown
        tk.Label(controls_frame, text="Dithering Method:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.combo_dither = ttk.Combobox(controls_frame, values=["Floyd-Steinberg", "Atkinson", "Threshold (No Dither)"], state="readonly")
        self.combo_dither.set("Floyd-Steinberg")
        self.combo_dither.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        self.combo_dither.bind("<<ComboboxSelected>>", self.update_preview)

        # Brightness Slider
        tk.Label(controls_frame, text="Brightness:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.slider_bright = tk.Scale(controls_frame, from_=0.3, to=2.5, resolution=0.1, orient=tk.HORIZONTAL, command=self.update_preview)
        self.slider_bright.set(1.1)
        self.slider_bright.grid(row=1, column=1, sticky="ew", padx=5, pady=2)

        # Contrast Slider
        tk.Label(controls_frame, text="Contrast:").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self.slider_contrast = tk.Scale(controls_frame, from_=0.5, to=3.0, resolution=0.1, orient=tk.HORIZONTAL, command=self.update_preview)
        self.slider_contrast.set(1.2)
        self.slider_contrast.grid(row=2, column=1, sticky="ew", padx=5, pady=2)

        # Hardware Thermal Energy (Print Density)
        tk.Label(controls_frame, text="Thermal Heat Density:").grid(row=3, column=0, sticky="w", padx=5, pady=2)
        self.slider_heat = tk.Scale(controls_frame, from_=10, to=100, resolution=5, orient=tk.HORIZONTAL)
        self.slider_heat.set(35)
        self.slider_heat.grid(row=3, column=1, sticky="ew", padx=5, pady=2)

        controls_frame.columnconfigure(1, weight=1)

        # 4. Status Bar & Action Buttons
        self.lbl_status = tk.Label(self.root, text="Status: Ready", font=("Arial", 10), fg="black")
        self.lbl_status.pack(pady=5)

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        self.btn_feed = tk.Button(btn_frame, text="Feed Paper", command=lambda: self.start_async_task(self.feed_paper_async), state=tk.DISABLED)
        self.btn_feed.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

        self.btn_print = tk.Button(btn_frame, text="Print Image", command=lambda: self.start_async_task(self.print_image_async), state=tk.DISABLED, bg="#4CAF50", fg="white", font=("Arial", 11, "bold"))
        self.btn_print.pack(side=tk.RIGHT, padx=5, expand=True, fill=tk.X)

    def select_image(self):
        file_types = [("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All Files", "*.*")]
        file_path = filedialog.askopenfilename(title="Select Image to Print", filetypes=file_types)

        if file_path:
            self.selected_image_path = file_path
            self.lbl_file.config(text=file_path.split("/")[-1], fg="black")
            self.update_preview()
            self.btn_print.config(state=tk.NORMAL)
            self.btn_feed.config(state=tk.NORMAL)

    def _apply_atkinson_dither(self, img_gray):
        """Custom Atkinson dithering implementation for sharper graphics."""
        pixels = img_gray.load()
        width, height = img_gray.size
        for y in range(height):
            for x in range(width):
                old_val = pixels[x, y]
                new_val = 255 if old_val > 127 else 0
                pixels[x, y] = new_val
                error = (old_val - new_val) >> 3
                
                # Distribute error to 6 neighboring pixels
                neighbors = [(x+1, y), (x+2, y), (x-1, y+1), (x, y+1), (x+1, y+1), (x, y+2)]
                for nx, ny in neighbors:
                    if 0 <= nx < width and 0 <= ny < height:
                        pixels[nx, ny] = max(0, min(255, pixels[nx, ny] + error))
        return img_gray.convert('1')

    def process_image(self, target_width=PRINTER_WIDTH) -> Image.Image:
        """Processes source image according to user adjustments into a 1-bit bitmap."""
        img = Image.open(self.selected_image_path).convert('L')

        # 1. Apply Brightness & Contrast
        img = ImageEnhance.Brightness(img).enhance(self.slider_bright.get())
        img = ImageEnhance.Contrast(img).enhance(self.slider_contrast.get())

        # 2. Rescale maintaining aspect ratio
        w_percent = target_width / float(img.size[0])
        target_height = int(float(img.size[1]) * float(w_percent))
        img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)

        # 3. Apply selected dither algorithm
        dither_choice = self.combo_dither.get()
        if dither_choice == "Floyd-Steinberg":
            return img.convert('1', dither=Image.FLOYDSTEINBERG)
        elif dither_choice == "Atkinson":
            return self._apply_atkinson_dither(img)
        else:
            # Simple Hard Threshold
            return img.point(lambda p: 255 if p > 127 else 0).convert('1')

    def update_preview(self, event=None):
        if not self.selected_image_path:
            return

        self.processed_mono_img = self.process_image()

        # Canvas Preview
        preview = self.processed_mono_img.copy()
        preview.thumbnail((380, 230))
        self.preview_tk_img = ImageTk.PhotoImage(preview)

        self.canvas.delete("all")
        self.canvas.create_image(192, 120, image=self.preview_tk_img)

    def build_print_job(self) -> bytes:
        img_bw = self.processed_mono_img
        raw_bytes = img_bw.tobytes()
        bytes_per_line = PRINTER_WIDTH // 8

        job_data = bytearray()

        # Setup commands
        job_data.extend(make_command(0xA1, b'\x01'))          # Quality speed setting
        job_data.extend(make_command(0xBE, b'\x00\x00'))      # Start session
        
        # User dynamic heat density
        heat_val = int(self.slider_heat.get())
        job_data.extend(make_command(0xAF, bytes([0x00, heat_val])))

        # Raster Scanlines
        for y in range(img_bw.height):
            start_idx = y * bytes_per_line
            line = raw_bytes[start_idx : start_idx + bytes_per_line]
            
            # Bit inversion (1=black dot) and bit reversal (endianness fix)
            processed_line = bytes(BIT_REVERSE_TABLE[~b & 0xFF] for b in line)
            job_data.extend(make_command(0xA2, processed_line))

        # Paper feed & termination
        job_data.extend(make_command(0xA1, b'\x50'))          # Feed ~80 dots
        job_data.extend(make_command(0xA3, b'\x01'))          # End session
        
        return bytes(job_data)

    async def _find_printer(self):
        """Scans for printer with retry mechanism."""
        self.lbl_status.config(text="Scanning for Bluetooth printer...", fg="blue")
        devices_and_adv = await BleakScanner.discover(timeout=5.0, return_adv=True)

        for device, adv_data in devices_and_adv.values():
            adv_uuids = [u.lower() for u in adv_data.service_uuids]
            
            uuid_match = any(t in adv_uuids for t in TARGET_SERVICE_UUIDS)
            name_match = device.name and any(k in device.name for k in ["X5", "MX", "Printer", "Tiny", "Cat", "GB01"])

            if uuid_match or name_match:
                return device
        return None

    async def feed_paper_async(self):
        printer = await self._find_printer()
        if not printer:
            messagebox.showerror("Error", "Printer not found!")
            self.lbl_status.config(text="Status: Device not found", fg="red")
            return

        try:
            self.lbl_status.config(text="Feeding paper...", fg="blue")
            async with BleakClient(printer.address, timeout=10.0) as client:
                feed_cmd = make_command(0xA1, b'\x80')  # Feed command
                await client.write_gatt_char(WRITE_UUID, feed_cmd, response=False)
                self.lbl_status.config(text="Status: Paper Fed", fg="green")
        except Exception as e:
            messagebox.showerror("Error", f"Feed error: {str(e)}")

    async def print_image_async(self):
        self.btn_print.config(state=tk.DISABLED)
        self.btn_feed.config(state=tk.DISABLED)

        printer = await self._find_printer()
        if not printer:
            messagebox.showerror("Error", "Printer not found!\nCheck Bluetooth & disconnect mobile app.")
            self.lbl_status.config(text="Status: Device not found", fg="red")
            self.btn_print.config(state=tk.NORMAL)
            self.btn_feed.config(state=tk.NORMAL)
            return

        self.lbl_status.config(text=f"Connecting to {printer.name or printer.address}...", fg="blue")

        try:
            payload = self.build_print_job()
            
            async with BleakClient(printer.address, timeout=12.0) as client:
                self.lbl_status.config(text="Printing image...", fg="green")
                
                # Flow Control Chunking
                chunk_size = 128
                for i in range(0, len(payload), chunk_size):
                    chunk = payload[i:i + chunk_size]
                    await client.write_gatt_char(WRITE_UUID, chunk, response=False)
                    await asyncio.sleep(0.015)

                messagebox.showinfo("Success", "Printing completed successfully!")
                self.lbl_status.config(text="Status: Print Success!", fg="green")

        except Exception as e:
            messagebox.showerror("Error", f"Printing failed: {str(e)}")
            self.lbl_status.config(text="Status: Print Failed", fg="red")

        finally:
            self.btn_print.config(state=tk.NORMAL)
            self.btn_feed.config(state=tk.NORMAL)

    def start_async_task(self, async_func):
        """Thread wrapper to run asyncio loops safely off Tkinter main loop."""
        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(async_func())

        threading.Thread(target=run_loop, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = ProTinyPrinterApp(root)
    root.mainloop()