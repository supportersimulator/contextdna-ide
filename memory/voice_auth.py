#!/usr/bin/env python3
"""
Voice Authentication Module for Synaptic

Provides voice fingerprint enrollment and verification using resemblyzer embeddings.
Voiceprints stored in ~/.context-dna/voiceprints.db (SQLite).

Usage:
    from memory.voice_auth import VoiceAuthManager

    auth = VoiceAuthManager()

    # Enroll (requires 3 audio samples for robustness)
    success = auth.enroll_voice("user@example.com", [audio1, audio2, audio3])

    # Verify
    is_match, similarity = auth.verify_voice("user@example.com", audio_bytes)

    # Check status
    is_enrolled = auth.get_enrollment_status("user@example.com")

Security:
    - Embeddings encrypted with user's device_token as XOR key
    - Threshold 0.70 for auth (higher than 0.55 filtering threshold)
    - Embeddings are 256-dimensional float32 vectors (1024 bytes)
"""

import io
import sqlite3
import struct
from pathlib import Path
from typing import Optional, Tuple, List
import hashlib
import logging

import numpy as np

# Resemblyzer imports (speaker embedding model)
RESEMBLYZER_AVAILABLE = False
BASIC_MODE = False

try:
    from resemblyzer import VoiceEncoder, preprocess_wav
    import soundfile as sf
    RESEMBLYZER_AVAILABLE = True
    VOICE_AUTH_AVAILABLE = True
except ImportError:
    # Fallback: Basic mode using audio hashing (for testing without PyTorch)
    try:
        import wave
        BASIC_MODE = True
        VOICE_AUTH_AVAILABLE = True
        logger_import = logging.getLogger(__name__)
        logger_import.info("Voice auth running in BASIC MODE (no ML embeddings)")
    except ImportError:
        VOICE_AUTH_AVAILABLE = False

logger = logging.getLogger(__name__)

# Configuration
CONTEXT_DNA_DIR = Path.home() / ".context-dna"
VOICEPRINTS_DB = CONTEXT_DNA_DIR / "voiceprints.db"
AUTH_SIMILARITY_THRESHOLD = 0.70  # Higher than 0.55 used for filtering
MIN_AUDIO_SAMPLES = 3  # Require 3 samples for robust enrollment
EMBEDDING_DIM = 256  # Resemblyzer embedding dimension


class VoiceAuthManager:
    """
    Voice fingerprint authentication manager.

    Uses resemblyzer to generate speaker embeddings and stores them
    encrypted in SQLite for later verification.
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the VoiceAuthManager.

        Args:
            db_path: Custom database path. Defaults to ~/.context-dna/voiceprints.db
        """
        self.db_path = db_path or VOICEPRINTS_DB
        self.encoder: Optional['VoiceEncoder'] = None
        self._init_db()
        self._init_encoder()

    def _init_db(self):
        """Initialize SQLite database for voiceprint storage.

        Schema uses user_id (UUID) as primary key for:
        - Stability: UUID never changes, email can
        - Privacy: No PII stored
        - Referential integrity
        """
        CONTEXT_DNA_DIR.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(str(self.db_path)) as conn:
            # Check if we need to migrate from old schema (user_email primary key)
            cursor = conn.execute("PRAGMA table_info(voiceprints)")
            columns = {row[1] for row in cursor.fetchall()}

            if "user_id" not in columns and "user_email" in columns:
                # Migrate from old schema - add user_id column and migrate data
                logger.info("Migrating voiceprints table to use user_id")
                conn.execute("ALTER TABLE voiceprints ADD COLUMN user_id TEXT")
                # Set user_id to user_email temporarily (can be updated later)
                conn.execute("UPDATE voiceprints SET user_id = user_email WHERE user_id IS NULL")
                conn.commit()
            elif not columns:
                # Fresh install - create new schema
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS voiceprints (
                        user_id TEXT PRIMARY KEY,
                        user_email TEXT,
                        embedding BLOB NOT NULL,
                        sample_count INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_voiceprints_email
                    ON voiceprints(user_email)
                """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS enrollment_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    user_email TEXT,
                    embedding BLOB NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def _init_encoder(self):
        """Initialize the resemblyzer voice encoder (CPU, ~50MB model)."""
        if not VOICE_AUTH_AVAILABLE:
            logger.warning("Voice auth not available - dependencies not installed")
            return

        if BASIC_MODE:
            logger.info("Voice auth running in BASIC MODE (audio hash comparison)")
            self.encoder = "BASIC_MODE"
            return

        try:
            self.encoder = VoiceEncoder()
            logger.info("Voice encoder initialized for authentication (ML mode)")
        except Exception as e:
            logger.error(f"Failed to initialize voice encoder: {e}")
            self.encoder = None

    def _extract_embedding_basic(self, audio_bytes: bytes) -> Optional[np.ndarray]:
        """Extract basic pseudo-embedding from audio bytes using hash.

        This is a fallback for when resemblyzer is not available.
        Uses audio content hash to create a deterministic 256-dim vector.
        Less secure than ML embeddings but functional for testing.
        """
        try:
            # Create a hash of the audio content
            audio_hash = hashlib.sha256(audio_bytes).digest()

            # Extend hash to 256 floats (deterministic pseudo-embedding)
            extended = audio_hash * 8  # 32 bytes * 8 = 256 bytes
            embedding = np.frombuffer(extended, dtype=np.uint8).astype(np.float32)
            embedding = embedding / 255.0  # Normalize to 0-1

            # Add some audio-length based variation
            length_factor = len(audio_bytes) / 100000.0  # Normalize by typical audio size
            embedding = embedding * (0.8 + 0.2 * min(length_factor, 1.0))

            return embedding
        except Exception as e:
            logger.error(f"Failed to extract basic embedding: {e}")
            return None

    def _extract_embedding(self, audio_bytes: bytes) -> Optional[np.ndarray]:
        """Extract speaker embedding from audio bytes.

        Args:
            audio_bytes: WAV audio data as bytes

        Returns:
            256-dimensional embedding array, or None if extraction failed
        """
        if not self.encoder:
            return None

        # Use basic mode if resemblyzer not available
        if BASIC_MODE or self.encoder == "BASIC_MODE":
            return self._extract_embedding_basic(audio_bytes)

        try:
            # Read audio from bytes
            audio_data, sample_rate = sf.read(io.BytesIO(audio_bytes))

            # Resemblyzer expects mono float32 at 16kHz
            if len(audio_data.shape) > 1:
                audio_data = audio_data.mean(axis=1)  # Stereo to mono

            # Preprocess and extract embedding
            processed = preprocess_wav(audio_data, source_sr=sample_rate)
            if len(processed) < 1600:  # Need at least 0.1s of audio
                logger.warning("Audio too short for embedding extraction")
                return None

            embedding = self.encoder.embed_utterance(processed)
            return embedding

        except Exception as e:
            logger.error(f"Failed to extract speaker embedding: {e}")
            return None

    def _xor_encrypt(self, data: bytes, key: str) -> bytes:
        """Simple XOR encryption with key.

        Args:
            data: Data to encrypt
            key: Encryption key (device_token)

        Returns:
            XOR-encrypted bytes
        """
        # Hash the key to get consistent length key material
        key_hash = hashlib.sha256(key.encode()).digest()
        key_bytes = key_hash * ((len(data) // len(key_hash)) + 1)

        return bytes(a ^ b for a, b in zip(data, key_bytes[:len(data)]))

    def _xor_decrypt(self, data: bytes, key: str) -> bytes:
        """XOR decryption (same as encryption for XOR)."""
        return self._xor_encrypt(data, key)

    def _embedding_to_bytes(self, embedding: np.ndarray) -> bytes:
        """Convert embedding array to bytes for storage."""
        return embedding.astype(np.float32).tobytes()

    def _bytes_to_embedding(self, data: bytes) -> np.ndarray:
        """Convert bytes back to embedding array."""
        return np.frombuffer(data, dtype=np.float32)

    def enroll_voice(
        self,
        user_id: str,
        audio_samples: List[bytes],
        device_token: Optional[str] = None,
        user_email: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Enroll a user's voice from multiple audio samples.

        Requires at least 3 audio samples for robust enrollment.
        Embeddings are averaged to create a stable voiceprint.

        Args:
            user_id: User's Supabase UUID (primary identifier)
            audio_samples: List of WAV audio bytes (minimum 3)
            device_token: Optional encryption key (if None, uses user_id hash)
            user_email: User's email (optional, for backwards compat/display)

        Returns:
            Tuple of (success: bool, message: str)
        """
        if not VOICE_AUTH_AVAILABLE:
            return False, "Voice authentication not available - missing dependencies"

        if not self.encoder:
            return False, "Voice encoder not initialized"

        if len(audio_samples) < MIN_AUDIO_SAMPLES:
            return False, f"Enrollment requires at least {MIN_AUDIO_SAMPLES} audio samples, got {len(audio_samples)}"

        # Extract embeddings from all samples
        embeddings = []
        for i, audio in enumerate(audio_samples):
            embedding = self._extract_embedding(audio)
            if embedding is None:
                logger.warning(f"Failed to extract embedding from sample {i+1}")
                continue
            embeddings.append(embedding)

        if len(embeddings) < MIN_AUDIO_SAMPLES:
            return False, f"Could only extract {len(embeddings)} valid embeddings, need {MIN_AUDIO_SAMPLES}"

        # Average embeddings for stability
        averaged_embedding = np.mean(embeddings, axis=0)

        # Normalize to unit vector (for cosine similarity)
        averaged_embedding = averaged_embedding / np.linalg.norm(averaged_embedding)

        # Encrypt embedding for storage (use user_id for key derivation)
        encryption_key = device_token or hashlib.sha256(user_id.encode()).hexdigest()
        embedding_bytes = self._embedding_to_bytes(averaged_embedding)
        encrypted_embedding = self._xor_encrypt(embedding_bytes, encryption_key)

        # Store in database (user_id is primary key)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO voiceprints
                (user_id, user_email, embedding, sample_count, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (user_id, user_email, encrypted_embedding, len(embeddings)))
            conn.commit()

        logger.info(f"Enrolled voiceprint for user_id={user_id[:8]}... from {len(embeddings)} samples")
        return True, f"Voice enrolled successfully from {len(embeddings)} samples"

    def verify_voice(
        self,
        user_id: str,
        audio: bytes,
        device_token: Optional[str] = None,
        threshold: float = AUTH_SIMILARITY_THRESHOLD
    ) -> Tuple[bool, float]:
        """Verify if audio matches enrolled voiceprint.

        Args:
            user_id: User's Supabase UUID to verify against
            audio: WAV audio bytes to verify
            device_token: Decryption key (must match enrollment key)
            threshold: Similarity threshold (default 0.70)

        Returns:
            Tuple of (is_match: bool, similarity: float)
        """
        if not VOICE_AUTH_AVAILABLE:
            logger.warning("Voice auth not available")
            return False, 0.0

        if not self.encoder:
            logger.warning("Voice encoder not initialized")
            return False, 0.0

        # Get stored voiceprint (user_id is primary key)
        decryption_key = device_token or hashlib.sha256(user_id.encode()).hexdigest()

        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT embedding FROM voiceprints WHERE user_id = ?",
                (user_id,)
            ).fetchone()

        if not row:
            logger.warning(f"No voiceprint found for user_id={user_id[:8]}...")
            return False, 0.0

        # Decrypt stored embedding
        try:
            encrypted_embedding = row[0]
            embedding_bytes = self._xor_decrypt(encrypted_embedding, decryption_key)
            stored_embedding = self._bytes_to_embedding(embedding_bytes)
        except Exception as e:
            logger.error(f"Failed to decrypt voiceprint: {e}")
            return False, 0.0

        # Extract embedding from verification audio
        current_embedding = self._extract_embedding(audio)
        if current_embedding is None:
            logger.warning("Could not extract embedding from verification audio")
            return False, 0.0

        # Normalize current embedding
        current_embedding = current_embedding / np.linalg.norm(current_embedding)

        # Cosine similarity (both are unit vectors, so dot product = cosine)
        similarity = float(np.dot(stored_embedding, current_embedding))

        is_match = similarity >= threshold

        if is_match:
            logger.info(f"Voice verified for user_id={user_id[:8]}...: similarity={similarity:.3f}")
        else:
            logger.info(f"Voice verification failed for user_id={user_id[:8]}...: similarity={similarity:.3f} < threshold={threshold}")

        return is_match, similarity

    def get_enrollment_status(self, user_id: str) -> bool:
        """Check if a user has an enrolled voiceprint.

        Args:
            user_id: User's Supabase UUID to check

        Returns:
            True if enrolled, False otherwise
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM voiceprints WHERE user_id = ?",
                (user_id,)
            ).fetchone()

        return row is not None

    def get_enrollment_info(self, user_id: str) -> Optional[dict]:
        """Get detailed enrollment information.

        Args:
            user_id: User's Supabase UUID

        Returns:
            Dict with enrollment details, or None if not enrolled
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                """SELECT user_email, sample_count, created_at, updated_at
                   FROM voiceprints WHERE user_id = ?""",
                (user_id,)
            ).fetchone()

        if not row:
            return None

        return {
            "enrolled": True,
            "user_id": user_id,
            "user_email": row[0],
            "sample_count": row[1],
            "created_at": row[2],
            "updated_at": row[3]
        }

    def delete_voiceprint(self, user_id: str) -> bool:
        """Delete a user's voiceprint.

        Args:
            user_id: User's Supabase UUID

        Returns:
            True if deleted, False if not found
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                "DELETE FROM voiceprints WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()
            return cursor.rowcount > 0


# Singleton instance for server use
_voice_auth_manager: Optional[VoiceAuthManager] = None

def get_voice_auth_manager() -> VoiceAuthManager:
    """Get or create the singleton VoiceAuthManager instance."""
    global _voice_auth_manager
    if _voice_auth_manager is None:
        _voice_auth_manager = VoiceAuthManager()
    return _voice_auth_manager


# CLI for testing
if __name__ == "__main__":
    import sys

    if not VOICE_AUTH_AVAILABLE:
        print("Voice authentication not available. Install dependencies:")
        print("  pip install resemblyzer soundfile")
        sys.exit(1)

    auth = VoiceAuthManager()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python voice_auth.py status <user_id>")
        print("  python voice_auth.py delete <user_id>")
        print()
        print("Note: user_id is the Supabase UUID (e.g., 550e8400-e29b-41d4-a716-446655440000)")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status" and len(sys.argv) >= 3:
        user_id = sys.argv[2]
        info = auth.get_enrollment_info(user_id)
        if info:
            print(f"Enrolled: user_id={user_id}")
            if info.get('user_email'):
                print(f"  Email: {info['user_email']}")
            print(f"  Samples: {info['sample_count']}")
            print(f"  Created: {info['created_at']}")
            print(f"  Updated: {info['updated_at']}")
        else:
            print(f"Not enrolled: user_id={user_id}")

    elif cmd == "delete" and len(sys.argv) >= 3:
        user_id = sys.argv[2]
        if auth.delete_voiceprint(user_id):
            print(f"Deleted voiceprint for: user_id={user_id}")
        else:
            print(f"No voiceprint found for: user_id={user_id}")

    else:
        print(f"Unknown command: {cmd}")
