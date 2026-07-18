import json
import os
import random
from typing import Mapping, Optional

from agent.profiler import load_profile, save_profile, utc_now_iso

WORD_BANK_PATH = os.path.join(os.path.dirname(__file__), "../data/word_bank.json")
DIAGNOSTIC_DIR = os.path.join(os.path.dirname(__file__), "../data/diagnostic_sessions")


def _load_word_bank() -> list[dict]:
    with open(WORD_BANK_PATH) as f:
        return json.load(f)["words"]


def load_diagnostic_session(student_id: str) -> dict:
    os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
    path = os.path.join(DIAGNOSTIC_DIR, f"{student_id}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    
    # Initialize a new session. Callers must verify profile consent before this
    # session is ever persisted.
    now = utc_now_iso()
    return {
        "student_id": student_id,
        "created_at": now,
        "updated_at": now,
        "question_index": 0,
        "max_questions": 10,
        "current_difficulty": 3,
        "history": [],
        "active_question": None,
        "completed": False,
        "starting_difficulty": None,
        "initial_phonics_struggles": {}
    }


def save_diagnostic_session(session: dict):
    # A persisted diagnostic is student data and therefore requires an existing,
    # consented profile.
    load_profile(session["student_id"], create_if_missing=False)
    session["updated_at"] = utc_now_iso()
    os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
    path = os.path.join(DIAGNOSTIC_DIR, f"{session['student_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2)
        f.write("\n")


def get_next_diagnostic_question(
    student_id: str,
    consent_metadata: Optional[Mapping[str, object]] = None,
) -> dict:
    # This must happen before a diagnostic session directory or file is created.
    load_profile(student_id, consent_metadata=consent_metadata)
    session = load_diagnostic_session(student_id)
    
    if session["completed"]:
        return {
            "completed": True,
            "student_id": student_id,
            "starting_difficulty": session["starting_difficulty"],
            "initial_phonics_struggles": session["initial_phonics_struggles"]
        }
        
    if session["active_question"] is not None:
        return {
            "completed": False,
            "student_id": student_id,
            "question_index": session["question_index"] + 1,
            "total_questions": session["max_questions"],
            "active_question": session["active_question"]
        }
        
    words = _load_word_bank()
    tested_words = {attempt["word"] for attempt in session["history"]}
    
    target_difficulty = session["current_difficulty"]
    candidates = [w for w in words if w["difficulty"] == target_difficulty and w["word"] not in tested_words]
    
    # Fallback to adjacent difficulties if none of the target difficulty are left
    if not candidates:
        for offset in [1, -1, 2, -2, 3, -3, 4, -4]:
            adj_diff = target_difficulty + offset
            if 1 <= adj_diff <= 5:
                candidates = [w for w in words if w["difficulty"] == adj_diff and w["word"] not in tested_words]
                if candidates:
                    break
                    
    # Ultimate fallback to any untested word
    if not candidates:
        candidates = [w for w in words if w["word"] not in tested_words]
        
    # If the bank is completely exhausted, finalize early
    if not candidates:
        session["completed"] = True
        _finalize_diagnostic(session)
        return {
            "completed": True,
            "student_id": student_id,
            "starting_difficulty": session["starting_difficulty"],
            "initial_phonics_struggles": session["initial_phonics_struggles"]
        }
        
    selected = random.choice(candidates)
    session["active_question"] = {
        "word": selected["word"],
        "difficulty": selected["difficulty"],
        "phonics": selected["phonics"],
        "theme": selected["theme"]
    }
    
    save_diagnostic_session(session)
    
    return {
        "completed": False,
        "student_id": student_id,
        "question_index": session["question_index"] + 1,
        "total_questions": session["max_questions"],
        "active_question": session["active_question"]
    }


def submit_diagnostic_answer(student_id: str, word: str, success: bool, time_taken_seconds: float) -> dict:
    # A diagnostic session cannot be submitted without its consented profile.
    profile = load_profile(student_id, create_if_missing=False)
    session = load_diagnostic_session(student_id)
    
    if session["completed"]:
        return {
            "completed": True,
            "student_id": student_id,
            "word": word,
            "success": success,
            "question_index": session["question_index"],
            "next_difficulty": session["current_difficulty"],
            "starting_difficulty": session["starting_difficulty"],
            "initial_phonics_struggles": session["initial_phonics_struggles"]
        }
        
    active = session["active_question"]
    if active is None:
        raise ValueError("No active diagnostic question to submit. Call next first.")
        
    if active["word"] != word:
        raise ValueError(f"Submitted word '{word}' does not match active diagnostic word '{active['word']}'.")
        
    attempt = {
        "word": active["word"],
        "difficulty": active["difficulty"],
        "phonics": active["phonics"],
        "theme": active["theme"],
        "success": success,
        "time_taken_seconds": time_taken_seconds
    }
    session["history"].append(attempt)
    
    # Adaptive stepping logic:
    # 1. success == True and fast (<= 10.0 seconds) -> Increase difficulty
    # 2. success == True and slow (> 10.0 seconds) -> Stay at same difficulty
    # 3. success == False -> Decrease difficulty
    current_diff = session["current_difficulty"]
    if success:
        if time_taken_seconds <= 10.0:
            next_diff = min(5, current_diff + 1)
        else:
            next_diff = current_diff
    else:
        next_diff = max(1, current_diff - 1)
        
    session["current_difficulty"] = next_diff
    session["question_index"] += 1
    session["active_question"] = None
    
    if session["question_index"] >= session["max_questions"]:
        session["completed"] = True
        _finalize_diagnostic(session)
    else:
        save_diagnostic_session(session)
        # A submitted answer is learning activity for retention purposes.
        save_profile(profile)
        
    return {
        "completed": session["completed"],
        "student_id": student_id,
        "word": word,
        "success": success,
        "question_index": session["question_index"],
        "next_difficulty": session["current_difficulty"],
        "starting_difficulty": session.get("starting_difficulty"),
        "initial_phonics_struggles": session.get("initial_phonics_struggles")
    }


def _finalize_diagnostic(session: dict):
    # Calculate initial phonics struggles based on failures in the diagnostic history
    initial_phonics_struggles = {}
    for attempt in session["history"]:
        if not attempt["success"]:
            for tag in attempt["phonics"]:
                initial_phonics_struggles[tag] = initial_phonics_struggles.get(tag, 0) + 1
                
    starting_diff = session["current_difficulty"]
    
    session["starting_difficulty"] = starting_diff
    session["initial_phonics_struggles"] = initial_phonics_struggles
    
    # Create or update student profile without polluting normal SM-2 review schedule
    student_id = session["student_id"]
    profile = load_profile(student_id, create_if_missing=False)
    profile["current_difficulty"] = starting_diff
    
    # Merge phonics struggles
    for tag, count in initial_phonics_struggles.items():
        profile["phonics_struggles"][tag] = profile["phonics_struggles"].get(tag, 0) + count
        
    # Store raw diagnostic attempts safely under 'diagnostic_history' to avoid SM-2 pollution
    profile["diagnostic_history"] = session["history"]
    
    save_profile(profile)
    save_diagnostic_session(session)
