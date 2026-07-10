FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Prevent python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install pip requirements
RUN pip install --no-cache-dir \
    Flask==3.0.0 \
    requests==2.31.0 \
    Pillow==10.1.0 \
    gunicorn==21.2.0

# Copy application files
COPY trmnl_loop.py .

# Expose server port
EXPOSE 5000

# Start server using gunicorn WSGI for production stability
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "trmnl_loop:app"]
