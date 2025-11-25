# Use official Python image
FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all files
COPY . .

# Expose port (Flask will run on this port)
EXPOSE 10000

# Run the Flask app
CMD ["python", "main.py"]
