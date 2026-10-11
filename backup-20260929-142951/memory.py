"""
Reflex System 1 Memory Engine (Skidnir-Style Recursive Learning).
Provides lightweight, sub-1ms incident logging and heuristic learning via SQLite.
Learns from prior execution errors and user/agent feedback to adjust model tiers autonomously.
"""
import os
import json
import sqlite3
import hashlib
import re
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

DEFAULT_DB_PATH = os.path.expanduser("~/.reflex/memory.sqlite3")

def get_db_path() -> str:
    db_path = os.getenv("REFLEX_MEMORY_DB", DEFAULT_DB_PATH)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return db_path

def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: Optional[str] = None) -> None:
    """Initializes the SQLite schema for incidents and metrics."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                prompt_hash TEXT NOT NULL,
                sample_text TEXT NOT NULL,
                shingles TEXT NOT NULL,
                failed_tier INTEGER NOT NULL,
                escalated_tier INTEGER NOT NULL,
                error_signature TEXT,
                hit_count INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_incidents_hash ON incidents(prompt_hash)
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tier_metrics (
                tier INTEGER PRIMARY KEY,
                request_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0
            )
        """)
        # Seed metrics table for tiers 0-3
        for t in range(4):
            cursor.execute("""
                INSERT OR IGNORE INTO tier_metrics (tier, request_count, error_count)
                VALUES (?, 0, 0)
            """, (t,))
        conn.commit()

def generate_shingles(text: str) -> List[str]:
    """
    Extracts normalized unigrams and bigrams from text for sub-millisecond Jaccard matching.
    """
    tokens = re.findall(r"\b[a-z0-9_]{2,}\b", text.lower())
    if not tokens:
        return []
    
    shingles: Set[str] = set()
    # Unigrams (important technical tokens)
    for tok in tokens:
        if len(tok) > 3:
            shingles.add(tok)
            
    # Bigrams
    for i in range(len(tokens) - 1):
        shingles.add(f"{tokens[i]}_{tokens[i+1]}")
        
    return sorted(list(shingles))

def record_incident(
    prompt: str,
    failed_tier: int,
    escalated_tier: int,
    error_signature: str = "",
    db_path: Optional[str] = None
) -> int:
    """
    Records an operational mistake or execution error.
    Returns the incident ID.
    """
    init_db(db_path)
    shingles = generate_shingles(prompt)
    prompt_hash = hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:16]
    sample = (prompt.strip()[:300] + "...") if len(prompt.strip()) > 300 else prompt.strip()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        # Check if identical hash already exists
        cursor.execute("SELECT id, hit_count FROM incidents WHERE prompt_hash = ?", (prompt_hash,))
        row = cursor.fetchone()
        if row:
            inc_id = row["id"]
            new_hits = row["hit_count"] + 1
            cursor.execute(
                "UPDATE incidents SET hit_count = ?, escalated_tier = MAX(escalated_tier, ?) WHERE id = ?",
                (new_hits, escalated_tier, inc_id)
            )
            conn.commit()
            return inc_id

        cursor.execute("""
            INSERT INTO incidents (
                timestamp, prompt_hash, sample_text, shingles, failed_tier, escalated_tier, error_signature, hit_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            time.time(),
            prompt_hash,
            sample,
            json.dumps(shingles),
            failed_tier,
            escalated_tier,
            error_signature
        ))
        inc_id = cursor.lastrowid
        # Update metric
        cursor.execute("UPDATE tier_metrics SET error_count = error_count + 1 WHERE tier = ?", (failed_tier,))
        conn.commit()
        return inc_id

def check_memory(
    prompt: str,
    threshold: float = 0.55,
    db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Performs instant online similarity check against known incidents.
    Returns matching incident details if similarity >= threshold, else None.
    """
    init_db(db_path)
    current_shingles = set(generate_shingles(prompt))
    if not current_shingles:
        return None

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, prompt_hash, sample_text, shingles, failed_tier, escalated_tier, error_signature, hit_count FROM incidents")
        rows = cursor.fetchall()

        best_match: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for row in rows:
            try:
                past_shingles = set(json.loads(row["shingles"]))
            except Exception:
                continue

            if not past_shingles:
                continue

            intersection = len(current_shingles.intersection(past_shingles))
            union = len(current_shingles.union(past_shingles))
            if union == 0:
                continue

            jaccard = intersection / union
            if jaccard > best_score:
                best_score = jaccard
                best_match = {
                    "incident_id": row["id"],
                    "similarity": round(jaccard, 3),
                    "failed_tier": row["failed_tier"],
                    "escalated_tier": row["escalated_tier"],
                    "error_signature": row["error_signature"],
                    "sample": row["sample_text"]
                }

        if best_match and best_score >= threshold:
            # Increment hit count
            cursor.execute("UPDATE incidents SET hit_count = hit_count + 1 WHERE id = ?", (best_match["incident_id"],))
            conn.commit()
            return best_match

    return None

def log_request(tier: int, db_path: Optional[str] = None) -> None:
    """Increments the request count for a given tier."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE tier_metrics SET request_count = request_count + 1 WHERE tier = ?", (tier,))
        conn.commit()

def get_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Returns memory metrics and incident count."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM incidents")
        total_incidents = cursor.fetchone()["cnt"]

        cursor.execute("SELECT tier, request_count, error_count FROM tier_metrics ORDER BY tier ASC")
        metrics = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT id, sample_text, failed_tier, escalated_tier, error_signature, hit_count FROM incidents ORDER BY timestamp DESC LIMIT 5")
        recent = [dict(r) for r in cursor.fetchall()]

    return {
        "total_incidents": total_incidents,
        "metrics": metrics,
        "recent_incidents": recent
    }
