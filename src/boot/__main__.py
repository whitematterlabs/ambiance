"""`python -m boot` entry — runs the full boot sequence."""
from .entry import boot
import sys

sys.exit(boot())
