# Base image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy project files
COPY . .

# Collect static files (optional, if using Django staticfiles)
RUN python manage.py collectstatic --noinput

# Start Gunicorn server
CMD ["gunicorn", "crp_final.wsgi:application", "--bind", "0.0.0.0:8000"]

