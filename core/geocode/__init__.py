"""
Geocoding sweep.

Reads projects with NULL lat/lng, calls a geocoder, and populates
lat / lng / county. Idempotent; safe to re-run after every reingest.
"""

from core.geocode.geocode import sweep_projects

__all__ = ["sweep_projects"]
