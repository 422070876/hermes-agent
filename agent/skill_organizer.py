"""Auto-organise skills into category directories using LLM classification.

Triggered once when Hermes detects unorganised skills at first session start
(or after upgrading from a version that predates category directories).
Runs inside the agent loop (LLM is available) and uses the current model to
classify each unorganised skill, then moves files and writes category metadata.

Lifecycle:
  1. ``needs_organize()`` — cheap filesystem scan (no LLM).
  2. ``organize_skills()`` — LLM classify + file moves + DESCRIPTION.md + sentinel.
  3. Subsequent starts: sentinel exists → skip entirely, zero cost.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home, get_skills_dir

logger = logging.getLogger(__name__)

# Sentinel file name placed under HERMES_HOME when organisation is done.
_ORGANIZED_SENTINEL = ".skills_organized"

# Regex to strip ANSI codes from LLM JSON responses (some models colourise output).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def needs_organize() -> bool:
    """Return True when there are unorganised skills AND no sentinel exists.

    An unorganised skill is a SKILL.md whose parent directory is NOT a
    recognised category — i.e. it lives directly under ``skills/<skill-name>/``
    with no category parent directory.

    The sentinel check ensures this only returns True **once** after install
    or upgrade.
    """
    if _organized_sentinel_exists():
        return False
    skills_dir = get_skills_dir()
    if not skills_dir.exists():
        return False
    for skill_file in _iter_unorganised_skills(skills_dir):
        return True  # At least one → needs organise
    # All skills are already organised — write sentinel so we never check again.
    _write_organized_sentinel()
    return False


def organize_skills(agent: Any) -> None:
    """Classify all unorganised skills via LLM and move them into place.

    Must be called after the LLM provider/model is ready but before the
    system prompt is built.  Mutates filesystem (creates dirs, moves files,
    writes DESCRIPTION.md, writes sentinel).  Idempotent *across runs* —
    once the sentinel is written this is a no-op.
    """
    skills_dir = get_skills_dir()
    if _organized_sentinel_exists():
        return
    if not skills_dir.exists():
        _write_organized_sentinel()
        return

    # Gather unorganised skills
    unorganised = list(_iter_unorganised_skills(skills_dir))
    if not unorganised:
        _write_organized_sentinel()
        logger.info("All skills already organised; wrote sentinel.")
        return

    # Gather existing category keywords from DESCRIPTION.md files
    existing_categories = _scan_existing_categories(skills_dir)

    # Build LLM prompt
    skill_list_lines = []
    for i, (skill_file, skill_name, desc) in enumerate(unorganised, 1):
        skill_list_lines.append(
            f"  {i}. name: {skill_name}\n     description: {desc[:200]}"
        )
    skill_list = "\n".join(skill_list_lines)

    cat_list_lines = []
    for cat, keywords in sorted(existing_categories.items()):
        kw_str = ", ".join(keywords[:8])
        cat_list_lines.append(f"  [{cat}] keywords: {kw_str}")
    cat_list = "\n".join(cat_list_lines) or "  (none yet — create new categories)"

    prompt = (
        "You are a Hermes Agent skill classifier. Classify each unorganised skill\n"
        "into one of the existing categories below, or propose a new category name\n"
        "if none fits.\n\n"
        "Existing categories:\n"
        f"{cat_list}\n\n"
        "Unorganised skills:\n"
        f"{skill_list}\n\n"
        'Respond with ONLY a JSON array of objects, no explanation:\n'
        '[\n'
        '  {"name": "<skill-name>", "category": "<category>"},\n'
        '  ...\n'
        ']\n'
        'Use lowercase-hyphenated category names.  Reuse existing categories when possible.\n'
        'For a new category, pick a short descriptive slug (e.g. "data-science", "automation").\n'
    )

    # Call LLM
    try:
        raw = _call_llm(agent, prompt)
        decisions = _parse_llm_response(raw)
    except Exception as exc:
        logger.error("Skill organisation LLM call failed: %s", exc)
        return

    if not decisions:
        logger.warning("LLM returned no skill classifications; skipping organise.")
        return

    # Execute moves
    category_keywords: Dict[str, set] = {}
    for cat, kw_list in existing_categories.items():
        category_keywords[cat] = set(kw_list)

    moved_count = 0
    for skill_file, skill_name, desc in unorganised:
        decision = decisions.get(skill_name)
        if not decision:
            logger.debug("No classification for %s; leaving in place.", skill_name)
            continue

        category = decision.strip().lower().replace(" ", "-").replace("_", "-")
        # Reject invalid category names
        if not category or not re.match(r"^[a-z0-9][a-z0-9/-]*$", category):
            logger.warning("Invalid category %r for %s; skipping.", category, skill_name)
            continue

        _move_skill(skill_file, skills_dir, category, skill_name)
        moved_count += 1

        # Collect keywords for new categories
        if category not in category_keywords:
            category_keywords[category] = set()
        category_keywords[category].add(skill_name)
        # Add description words as keywords too
        for word in _extract_keywords_from_desc(desc):
            category_keywords[category].add(word)

    # Ensure every category has a DESCRIPTION.md with keywords
    if moved_count:
        _ensure_category_descriptions(skills_dir, category_keywords, existing_categories)

    # Write sentinel
    _write_organized_sentinel()
    logger.info(
        "Skill organisation complete: %d/%d skills classified into %d categories.",
        moved_count, len(unorganised), len(category_keywords),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _organized_sentinel_path() -> Path:
    """Return the path to the organised sentinel under HERMES_HOME."""
    return get_hermes_home() / _ORGANIZED_SENTINEL


def _organized_sentinel_exists() -> bool:
    return _organized_sentinel_path().exists()


def _write_organized_sentinel() -> None:
    try:
        _organized_sentinel_path().write_text("1", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write organisation sentinel: %s", exc)


def _clear_organized_sentinel() -> None:
    """Test helper / re-trigger."""
    try:
        _organized_sentinel_path().unlink(missing_ok=True)
    except OSError:
        pass


def _iter_unorganised_skills(skills_dir: Path):
    """Yield (skill_file_path, skill_name, description) for unorganised skills.

    A skill is "unorganised" when its SKILL.md lives directly under
    ``skills/<skill-name>/SKILL.md`` (no category parent dir).

    Yields:
        Tuple[Path, str, str]: (SKILL.md path, skill directory name, description)
    """
    from agent.skill_utils import iter_skill_index_files, parse_frontmatter

    for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
        rel = skill_file.relative_to(skills_dir)
        parts = rel.parts

        # Organised: skills/<category>/<skill-name>/SKILL.md  (3+ parts)
        # Unorganised: skills/<skill-name>/SKILL.md            (2 parts)
        if len(parts) >= 3:
            continue  # Already in a category dir

        # Check frontmatter for a category override
        try:
            raw = skill_file.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(raw)
            fm_category = frontmatter.get("category", "")
            if fm_category:
                # Has frontmatter category but wrong folder → also unorganised
                pass  # Will be moved to the correct folder
        except Exception:
            frontmatter = {}

        skill_name = parts[-2]
        description = _get_skill_description(skill_file, frontmatter)
        yield skill_file, skill_name, description


def _get_skill_description(skill_file: Path, frontmatter: dict) -> str:
    """Get the best available description for a skill."""
    desc = frontmatter.get("description", "")
    if desc and isinstance(desc, str):
        return desc.strip()
    # Fallback: first non-header line of the body
    try:
        from agent.skill_utils import parse_frontmatter as _pf
        _, body = _pf(skill_file.read_text(encoding="utf-8"))
        for line in body.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:200]
    except Exception:
        pass
    return skill_file.parent.name


def _scan_existing_categories(skills_dir: Path) -> Dict[str, List[str]]:
    """Scan existing category DESCRIPTION.md files for their keywords."""
    from agent.skill_utils import parse_frontmatter

    result: Dict[str, List[str]] = {}
    for desc_file in skills_dir.rglob("DESCRIPTION.md"):
        if desc_file.parent == skills_dir:
            continue  # Skip a top-level DESCRIPTION.md
        try:
            content = desc_file.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(content)
            keywords = fm.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [keywords]
            if not isinstance(keywords, list):
                keywords = []
            keywords = [str(k).strip().lower() for k in keywords if str(k).strip()]
            rel = desc_file.parent.relative_to(skills_dir)
            # category name is the path from skills_dir to the desc file
            cat_parts = rel.parts
            cat_name = "/".join(cat_parts)
            result[cat_name] = keywords
        except Exception as e:
            logger.debug("Could not read %s: %s", desc_file, e)

    return result


def _extract_keywords_from_desc(description: str) -> List[str]:
    """Pull significant keywords from a description string."""
    # Simple heuristic: lowercase, split, keep words >= 4 chars that aren't
    # common stopwords
    stopwords = {
        "this", "that", "with", "from", "have", "been", "they", "them",
        "their", "will", "would", "could", "should", "about", "into",
        "over", "than", "then", "also", "only", "very", "just", "more",
        "some", "such", "each", "well", "used", "using", "based", "tool",
        "file", "code", "data", "work", "like", "make", "made",
    }
    words = re.findall(r"[a-zA-Z]{4,}", description.lower())
    return list(dict.fromkeys(w for w in words if w not in stopwords))


def _move_skill(
    skill_file: Path, skills_dir: Path, category: str, skill_name: str
) -> None:
    """Move a SKILL.md (and its supporting files) into the category directory."""
    # Normalise category path (support subcategories like "mlops/inference")
    cat_path = category.replace("-", "/")
    target_dir = skills_dir / cat_path / skill_name
    source_dir = skill_file.parent

    if source_dir.resolve() == target_dir.resolve():
        logger.debug("Skill %s already in %s; skipping move.", skill_name, category)
        return

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Move all files from source to target
    for item in source_dir.iterdir():
        dest = target_dir / item.name
        if item.is_dir():
            if dest.exists():
                # Merge directories if dest exists (unlikely but safe)
                for sub in item.rglob("*"):
                    if sub.is_file():
                        rel_sub = sub.relative_to(item)
                        sub_dest = dest / rel_sub
                        sub_dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sub, sub_dest)
                shutil.rmtree(item)
            else:
                shutil.move(str(item), str(dest))
        else:
            shutil.move(str(item), str(dest))

    # Remove source directory if empty
    try:
        if source_dir.exists() and not any(source_dir.iterdir()):
            source_dir.rmdir()
    except OSError:
        pass

    logger.info("Moved skill '%s' → [%s]", skill_name, category)


def _ensure_category_descriptions(
    skills_dir: Path,
    category_keywords: Dict[str, set],
    existing_categories: Dict[str, List[str]],
) -> None:
    """Write or update DESCRIPTION.md for every category that received a skill."""
    from agent.skill_utils import parse_frontmatter

    for category, keywords in sorted(category_keywords.items()):
        cat_path = skills_dir / category
        desc_file = cat_path / "DESCRIPTION.md"

        # Compute a rough description from skill names
        if not desc_file.exists():
            description = _infer_category_description(category, list(keywords))
            yaml_keywords = sorted(
                kw.replace("'", "") for kw in keywords if len(kw) >= 3
            )
            lines = [
                "---",
                f"description: {description}",
                f"keywords: {json.dumps(yaml_keywords, ensure_ascii=False)}",
                "---",
                "",
            ]
            desc_file.write_text("\n".join(lines), encoding="utf-8")
            logger.info("Created category description: [%s]", category)
        else:
            # Update keywords if new ones arrived
            try:
                content = desc_file.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(content)
                existing_keywords = set(fm.get("keywords", []) or [])
                new_keywords = keywords - existing_keywords
                if new_keywords:
                    merged = sorted(existing_keywords | keywords)
                    kw_line = f"keywords: {json.dumps(merged, ensure_ascii=False)}"
                    # Replace the keywords line in the file
                    import re as _re
                    new_content = _re.sub(
                        r"^keywords:.*$",
                        kw_line,
                        content,
                        flags=_re.MULTILINE,
                    )
                    if new_content != content:
                        desc_file.write_text(new_content, encoding="utf-8")
                        logger.info(
                            "Updated keywords for [%s]: +%d",
                            category, len(new_keywords),
                        )
            except Exception:
                pass


def _infer_category_description(category: str, keywords: list[str]) -> str:
    """Generate a human-readable description for a new category."""
    # Try to derive from category name
    name_parts = category.replace("-", " ").replace("/", " ").title()
    # Pick the most descriptive keywords
    top_kw = [k for k in keywords if len(k) > 4][:5]
    kw_summary = ", ".join(top_kw) if top_kw else category
    return f"{name_parts} skills — {kw_summary} and related workflows."


def _call_llm(agent: Any, prompt: str) -> str:
    """Call the agent's LLM and return the raw response text.

    Uses the agent's provider/model/client directly so the classification
    call goes through the same credential pool, fallback chain, and retry
    loop the agent uses for normal turns.
    """
    try:
        # Use the agent's run_llm method which handles the full lifecycle:
        # provider resolution, credential pool, retry, fallback.
        response = agent.run_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.1,  # Low temperature for classification consistency
        )
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            return response.get("content", "") or response.get("text", "") or json.dumps(response)
        return str(response)
    except AttributeError:
        # Fallback for older agent interfaces
        try:
            from agent.conversation_loop import _resolve_llm_call
            response = _resolve_llm_call(agent, prompt, max_tokens=2000, temperature=0.1)
            return str(response)
        except Exception as exc:
            raise RuntimeError(f"LLM call failed (attribute error): {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc


def _parse_llm_response(raw: str) -> Dict[str, str]:
    """Parse LLM JSON response into {skill_name: category} mapping.

    Accepts a JSON array directly, or a JSON array embedded in a code
    fence / markdown block.
    """
    raw = _ANSI_RE.sub("", raw)

    # Try to extract JSON array from markdown code fence first
    json_match = re.search(
        r"```(?:json)?\s*\n(\[[\s\S]*?\])\n\s*```", raw
    )
    if json_match:
        raw_json = json_match.group(1)
    else:
        # Try bare JSON array
        array_match = re.search(r"(\[[\s\S]*?\])", raw)
        if array_match:
            raw_json = array_match.group(1)
        else:
            # Try JSON object (single dict) as fallback
            obj_match = re.search(r"(\{[\s\S]*?\})", raw)
            if obj_match:
                raw_json = obj_match.group(1)
            else:
                logger.warning("No JSON found in LLM response: %.200s", raw)
                return {}

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse LLM JSON: %s\nRaw: %.200s", exc, raw_json)
        return {}

    if isinstance(data, dict):
        # {"skill-name": "category", ...}
        return {str(k).strip(): str(v).strip() for k, v in data.items()}

    if isinstance(data, list):
        result = {}
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                category = str(item.get("category", "")).strip()
                if name and category:
                    result[name] = category
        return result

    return {}


# ---------------------------------------------------------------------------
# CLI command helper (optional: `hermes skills organize`)
# ---------------------------------------------------------------------------


def organize_skills_cli(agent: Any = None) -> str:
    """Run organisation and return a human-readable summary.

    Can be called from a CLI command handler.  When *agent* is None,
    only checks if organisation is needed (no LLM call).
    """
    if not needs_organize():
        return "All skills are already organised."

    if agent is None:
        return "Skills need organisation. Run `hermes skills organize` with an active session to classify."

    organize_skills(agent)
    return "Skill organisation complete. New session will use categorised layout."