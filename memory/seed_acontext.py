"""
DEPRECATED: Use seed_context_dna.py instead

This file is kept for backwards compatibility.
All functionality has been moved to seed_context_dna.py
"""

# Re-export from new module
from memory.seed_context_dna import *

if __name__ == "__main__":
    print("⚠️  seed_acontext.py is deprecated. Use seed_context_dna.py instead.")
    print("   Running: python memory/seed_context_dna.py")
    print()
    from memory.seed_context_dna import main
    main()
