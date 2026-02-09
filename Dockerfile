# ใช้ Python 3.9 แบบ Slim เพื่อให้ Image ขนาดเล็ก
FROM python:3.9-slim

# ตั้งค่า Working Directory
WORKDIR /app

# copy file requirements.txt ไปที่ container
COPY requirements.txt .

# ติดตั้ง dependencies
RUN pip install --no-cache-dir -r requirements.txt

# copy code ทั้งหมดไปที่ container
COPY . .

# สร้างโฟลเดอร์สำหรับเก็บ Database (ถ้าจำเป็น)
RUN mkdir -p /app/data

# เปิด Port 8501 (Default ของ Streamlit)
EXPOSE 8501

# รันคำสั่งเริ่มแอป
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
