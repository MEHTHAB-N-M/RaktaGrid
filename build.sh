#!/usr/bin/env bash
# Exit on error
set -o errexit

# Install dependencies
pip install -r requirements.txt

# Collect static assets
python manage.py collectstatic --no-input

# Run database migrations
python manage.py migrate

# Create database cache table for rate-limiting
python manage.py createcachetable

# Seed fictional demo hospitals, blood banks, and donors
python manage.py seed_demo --reset
