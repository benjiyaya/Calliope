# Calliope backend package

# Single source of truth for the backend version. main.py (FastAPI + /api/health)
# reads this; pyproject.toml is kept in sync for the build (hatchling dynamic
# version reads this file). PR #47: hard-coded literals drifted twice
# (1.3.2 shipped reporting 1.2.1; 1.4.1's /api/health still said 1.4.0).
__version__ = "1.5.4"
