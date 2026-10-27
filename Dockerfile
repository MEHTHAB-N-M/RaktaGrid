FROM python:3.12-slim

# Prevent Python from writing .pyc and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . /app/

# Hugging Face Spaces requires running as UID 1000
RUN useradd -m -u 1000 user && \
    chown -R user:user /app

USER user

EXPOSE 7860

# Run migrations, collect static, seed data, and start Gunicorn
CMD ["sh", "-c", "python manage.py collectstatic --no-input && python manage.py migrate && python manage.py createcachetable && python manage.py seed_demo --reset && gunicorn ebdf_project.wsgi:application --bind 0.0.0.0:7860 --workers 3"]
